"""
収支マイナス原因の切り分け用診断API。

方針:
- 予想モデル・候補生成は変更しない
- Purchase / SkippedBet の既存記録から「確率→EV→購入→実績」を分離計測する
- 閾値やフィルタの自動最適化は行わない(読み取り専用)

期間: since=calibration_switch (既定) または ISO 日時
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models
from .. import ev_calculator as calc
from . import purchases as purchases_router

router = APIRouter(prefix="/purchases/diagnostics", tags=["purchase-diagnostics"])

# EV回収率帯(ev_pct_at_purchase + 100 で回収率換算)
EV_BANDS = [
    ("EV100%未満", None, 0.0),
    ("100-105%", 0.0, 5.0),
    ("105-110%", 5.0, 10.0),
    ("110-120%", 10.0, 20.0),
    ("120-150%", 20.0, 50.0),
    ("150%以上", 50.0, None),
]
MIN_BAND_N = 30  # これ未満は結論禁止
MIN_PURCHASE_FOR_PROFIT_JUDGE = 50


def _since_dt(since: Optional[str]) -> Optional[datetime]:
    return purchases_router._parse_since_param(since or "calibration_switch")


def _filter_by_created(q, model, since_dt: Optional[datetime]):
    if since_dt is not None and hasattr(model, "created_at"):
        return q.filter(model.created_at >= since_dt)
    return q


def _load_settled_purchases(db: Session, since_dt: Optional[datetime]) -> List[models.Purchase]:
    # Purchaseにrace relationshipは無い。日付はpurchased_atを使う。
    q = db.query(models.Purchase).filter(models.Purchase.result.in_(("win", "lose")))
    if since_dt is not None:
        q = q.filter(models.Purchase.purchased_at >= since_dt)
    return q.all()


def _load_settled_skips(db: Session, since_dt: Optional[datetime]) -> List[models.SkippedBet]:
    # SkippedBetにcreated_atが無いため、レース開催日で絞る。
    q = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.in_(("win", "lose")))
    )
    if since_dt is not None:
        q = (
            q.join(models.Race, models.Race.id == models.SkippedBet.race_id)
            .filter(models.Race.race_date >= since_dt)
        )
    return q.all()


def _band_for_ev_pct(ev_pct: Optional[float]) -> str:
    if ev_pct is None:
        return "EV不明"
    for label, lo, hi in EV_BANDS:
        if lo is None and ev_pct < hi:
            return label
        if hi is None and ev_pct >= lo:
            return label
        if lo is not None and hi is not None and lo <= ev_pct < hi:
            return label
    return "EV不明"


def _agg_rows(
    rows: List[dict],
    stake_key: str = "stake",
    payout_key: str = "payout",
    won_key: str = "won",
) -> dict:
    n = len(rows)
    if n == 0:
        return {
            "bet_count": 0,
            "race_count": 0,
            "hit_count": 0,
            "actual_hit_rate_pct": None,
            "predicted_avg_probability_pct": None,
            "average_odds": None,
            "predicted_average_ev_pct": None,
            "actual_return": 0.0,
            "actual_profit": 0.0,
            "actual_roi_pct": None,
            "n_insufficient": True,
        }
    stake = sum(r[stake_key] for r in rows)
    payout = sum(r[payout_key] for r in rows)
    hits = sum(1 for r in rows if r[won_key])
    probs = [r["prob"] for r in rows if r.get("prob") is not None]
    odds = [r["odds"] for r in rows if r.get("odds") is not None]
    evs = [r["ev_pct"] for r in rows if r.get("ev_pct") is not None]
    races = {r["race_id"] for r in rows if r.get("race_id") is not None}
    return {
        "bet_count": n,
        "race_count": len(races),
        "hit_count": hits,
        "actual_hit_rate_pct": round(100.0 * hits / n, 4),
        "predicted_avg_probability_pct": round(100.0 * (sum(probs) / len(probs)), 4) if probs else None,
        "average_odds": round(sum(odds) / len(odds), 4) if odds else None,
        "predicted_average_ev_pct": round(sum(evs) / len(evs), 4) if evs else None,
        "actual_return": round(payout, 2),
        "actual_profit": round(payout - stake, 2),
        "actual_roi_pct": round(100.0 * payout / stake, 4) if stake > 0 else None,
        "n_insufficient": n < MIN_BAND_N,
    }


def _purchase_row(p: models.Purchase) -> dict:
    stake = float(p.stake_amount or 0)
    payout = float(p.payout_amount or 0)
    odds = p.odds_at_purchase
    if odds is None and stake > 0 and p.result == "win" and payout > 0:
        odds = payout / stake
    prob = p.win_prob_at_purchase
    if prob is None:
        prob = p.win_prob_raw
    return {
        "race_id": p.race_id,
        "bet_type": p.bet_type,
        "combination": p.combination,
        "won": p.result == "win",
        "stake": stake,
        "payout": payout,
        "odds": float(odds) if odds is not None else None,
        "prob": float(prob) if prob is not None else None,
        "prob_raw": float(p.win_prob_raw) if p.win_prob_raw is not None else None,
        "prob_cal": float(p.win_prob_at_purchase) if p.win_prob_at_purchase is not None else None,
        "ev_pct": float(p.ev_pct_at_purchase) if p.ev_pct_at_purchase is not None else None,
        "final_odds": float(p.final_odds) if getattr(p, "final_odds", None) is not None else None,
        "source": "purchase",
        "skip_reason": None,
        "skip_category": None,
    }


def _skip_row(s: models.SkippedBet, default_stake: float) -> dict:
    won = s.actual_result == "win"
    # 見送りは当時stakeが無い → 同一券種の平均stake、無ければ100円
    stake = default_stake if default_stake > 0 else 100.0
    payout = float(s.actual_payout) if s.actual_payout is not None else (stake if won and False else 0.0)
    # actual_payout が倍率で入っている場合と金額の場合があるため:
    # 金額として保存されていればそのまま、nullで的中なら odds 不明で0扱い(過小評価を明示)
    if s.actual_payout is not None:
        payout = float(s.actual_payout)
        # もし払戻が「倍率」っぽく小さい値で stake が100なら stake*倍率の可能性は呼び出し側で調整しない
        # 既存実装では actual_payout は金額想定
    else:
        payout = 0.0
    odds = None
    if won and stake > 0 and payout > 0:
        odds = payout / stake
    prob = s.win_prob_estimated if s.win_prob_estimated is not None else s.win_prob_raw
    return {
        "race_id": s.race_id,
        "bet_type": s.bet_type,
        "combination": s.combination,
        "won": won,
        "stake": stake,
        "payout": payout,
        "odds": odds,
        "prob": float(prob) if prob is not None else None,
        "prob_raw": float(s.win_prob_raw) if s.win_prob_raw is not None else None,
        "prob_cal": float(s.win_prob_estimated) if s.win_prob_estimated is not None else None,
        "ev_pct": float(s.ev_pct_estimated) if s.ev_pct_estimated is not None else None,
        "final_odds": None,
        "source": "skipped",
        "skip_reason": s.reason,
        "skip_category": purchases_router._categorize_skip_reason(s.reason or ""),
    }


def _default_stakes_by_type(purchases: List[models.Purchase]) -> Dict[str, float]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    for p in purchases:
        if p.stake_amount and p.stake_amount > 0:
            buckets[p.bet_type].append(float(p.stake_amount))
    return {k: (sum(v) / len(v)) for k, v in buckets.items()}


def _ev_rank_correlation(band_stats: List[dict]) -> dict:
    """サンプル十分な帯だけで、帯の中央EV順と実績ROIの単調性を粗い判定。"""
    usable = [b for b in band_stats if not b.get("n_insufficient") and b.get("actual_roi_pct") is not None]
    if len(usable) < 3:
        return {
            "ev_rank_correlation": "inconclusive",
            "note": "サンプル十分なEV帯が3未満のため判定不能",
        }
    rois = [b["actual_roi_pct"] for b in usable]
    # 帯は低いEVから高いEVの順に並べている前提
    ups = sum(1 for i in range(1, len(rois)) if rois[i] > rois[i - 1])
    downs = sum(1 for i in range(1, len(rois)) if rois[i] < rois[i - 1])
    if ups >= downs + 1:
        status = "positive"
        note = "EV帯が上がるほど実績ROIが改善する傾向"
    elif downs >= ups + 1:
        status = "broken"
        note = "EV帯が上がっても実績ROIが改善しない。EV計算・確率・オッズ・キャリブのいずれかに問題の可能性"
    else:
        status = "flat"
        note = "EV帯と実績ROIに明確な単調関係が見えない"
    return {"ev_rank_correlation": status, "note": note, "rois_by_band_order": rois}


@router.get("/ev-bands")
def diagnostics_ev_bands(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題1・2: EV帯別・券種別の実績ROI。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    rows = [_purchase_row(p) for p in purchases]

    def build_bands(subset: List[dict]) -> List[dict]:
        out = []
        for label, lo, hi in EV_BANDS:
            bucket = []
            for r in subset:
                ev = r.get("ev_pct")
                if ev is None:
                    continue
                if lo is None and ev < hi:
                    bucket.append(r)
                elif hi is None and ev >= lo:
                    bucket.append(r)
                elif lo is not None and hi is not None and lo <= ev < hi:
                    bucket.append(r)
            stat = _agg_rows(bucket)
            stat["band"] = label
            out.append(stat)
        # EV不明
        unknown = [r for r in subset if r.get("ev_pct") is None]
        if unknown:
            st = _agg_rows(unknown)
            st["band"] = "EV不明"
            out.append(st)
        return out

    overall_bands = build_bands(rows)
    by_type = {}
    for bt in sorted({r["bet_type"] for r in rows}):
        by_type[bt] = build_bands([r for r in rows if r["bet_type"] == bt])

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "purchase_count": len(rows),
        "overall": {
            "bands": overall_bands,
            **_ev_rank_correlation(overall_bands),
        },
        "by_bet_type": {
            bt: {"bands": bands, **_ev_rank_correlation(bands)} for bt, bands in by_type.items()
        },
        "note": (
            "predicted_average_ev_pctはev_pct_at_purchaseの平均(0=損益分岐)。"
            "actual_roi_pctは払戻÷投資×100。"
            f"件数{MIN_BAND_N}未満の帯はn_insufficient=trueで結論禁止。"
        ),
    }


@router.get("/raw-vs-calibrated")
def diagnostics_raw_vs_calibrated(
    since: Optional[str] = Query("calibration_switch"),
    min_ev_pct: float = Query(0.0, description="仮想購入の最低EV%(既存と同様0以上)"),
    min_win_prob: float = Query(0.0, description="仮想購入の最低勝率0-1"),
    db: Session = Depends(get_db),
):
    """課題3・4: 同一候補でRaw/Calibratedの仮想購入ROIを比較。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)
    stake_map = _default_stakes_by_type(purchases)

    universe: List[dict] = []
    for p in purchases:
        universe.append(_purchase_row(p))
    for s in skips:
        universe.append(_skip_row(s, stake_map.get(s.bet_type, 100.0)))

    def simulate(mode: str) -> Tuple[List[dict], dict]:
        chosen = []
        for r in universe:
            if mode == "raw":
                prob = r.get("prob_raw")
            else:
                prob = r.get("prob_cal") if r.get("prob_cal") is not None else r.get("prob")
            odds = r.get("odds")
            if prob is None or odds is None or odds <= 0:
                continue
            ev = calc.calc_ev_pct(prob, odds)
            if ev < min_ev_pct:
                continue
            if prob < min_win_prob:
                continue
            chosen.append({**r, "prob": prob, "ev_pct": ev, "stake": r["stake"] or 100.0})
        return chosen, _agg_rows(chosen)

    raw_rows, raw_agg = simulate("raw")
    cal_rows, cal_agg = simulate("cal")

    raw_keys = {(r["race_id"], r["bet_type"], r["combination"]) for r in raw_rows}
    cal_keys = {(r["race_id"], r["bet_type"], r["combination"]) for r in cal_rows}

    by_type = {}
    for bt in sorted({r["bet_type"] for r in universe}):
        sub = [r for r in universe if r["bet_type"] == bt]
        # local simulate
        def sim(mode, subset):
            chosen = []
            for r in subset:
                prob = r.get("prob_raw") if mode == "raw" else (
                    r.get("prob_cal") if r.get("prob_cal") is not None else r.get("prob")
                )
                odds = r.get("odds")
                if prob is None or odds is None or odds <= 0:
                    continue
                ev = calc.calc_ev_pct(prob, odds)
                if ev < min_ev_pct or prob < min_win_prob:
                    continue
                chosen.append({**r, "prob": prob, "ev_pct": ev})
            return _agg_rows(chosen)

        raw_a = sim("raw", sub)
        cal_a = sim("cal", sub)
        # Brier
        raw_pairs = [(r["prob_raw"], 1.0 if r["won"] else 0.0) for r in sub if r.get("prob_raw") is not None]
        cal_pairs = [
            (
                r["prob_cal"] if r.get("prob_cal") is not None else r.get("prob"),
                1.0 if r["won"] else 0.0,
            )
            for r in sub
            if (r.get("prob_cal") is not None or r.get("prob") is not None)
        ]
        raw_pairs = [(p, w) for p, w in raw_pairs if p is not None]
        cal_pairs = [(p, w) for p, w in cal_pairs if p is not None]

        def brier(pairs):
            if not pairs:
                return None
            return round(sum((p - w) ** 2 for p, w in pairs) / len(pairs), 6)

        def deviation(pairs):
            if not pairs:
                return None
            act = sum(w for _, w in pairs) / len(pairs)
            pred = sum(p for p, _ in pairs) / len(pairs)
            return round(100.0 * (act - pred), 4)

        raw_roi = raw_a.get("actual_roi_pct")
        cal_roi = cal_a.get("actual_roi_pct")
        if raw_a["bet_count"] < MIN_BAND_N or cal_a["bet_count"] < MIN_BAND_N:
            verdict = "inconclusive"
        elif cal_roi is not None and raw_roi is not None:
            if cal_roi > raw_roi + 1:
                verdict = "improved"
            elif cal_roi < raw_roi - 1:
                verdict = "worsened"
            else:
                verdict = "similar"
        else:
            verdict = "inconclusive"

        by_type[bt] = {
            "raw": raw_a,
            "calibrated": cal_a,
            "brier_raw": brier(raw_pairs),
            "brier_calibrated": brier(cal_pairs),
            "deviation_pt_raw": deviation(raw_pairs),
            "deviation_pt_calibrated": deviation(cal_pairs),
            "calibration_verdict": verdict,
            "note": "verdictは仮想ROI比較(Brier単独では判定しない)",
        }

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "universe_count": len(universe),
        "min_ev_pct": min_ev_pct,
        "min_win_prob": min_win_prob,
        "overall": {
            "raw": raw_agg,
            "calibrated": cal_agg,
            "raw_only_count": len(raw_keys - cal_keys),
            "calibrated_only_count": len(cal_keys - raw_keys),
            "both_count": len(raw_keys & cal_keys),
        },
        "by_bet_type": by_type,
    }


@router.get("/filter-effectiveness")
def diagnostics_filter_effectiveness(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題7・8・15: 購入群 vs 除外群、reason段階別の仮想ROI。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)
    stake_map = _default_stakes_by_type(purchases)

    purchased_rows = [_purchase_row(p) for p in purchases]
    skip_rows = [_skip_row(s, stake_map.get(s.bet_type, 100.0)) for s in skips]

    # EVマイナス除外群
    ev_neg = [
        r for r in skip_rows
        if (r.get("skip_category") or "").startswith("購入判断:期待値マイナス")
        or (r.get("ev_pct") is not None and r["ev_pct"] < 0)
    ]

    def by_type_compare(purchased, excluded):
        types = sorted({r["bet_type"] for r in purchased + excluded})
        out = {}
        for bt in types:
            out[bt] = {
                "purchased": _agg_rows([r for r in purchased if r["bet_type"] == bt]),
                "ev_negative_excluded": _agg_rows([r for r in excluded if r["bet_type"] == bt]),
            }
            pr = out[bt]["purchased"].get("actual_roi_pct")
            er = out[bt]["ev_negative_excluded"].get("actual_roi_pct")
            if (
                out[bt]["purchased"]["bet_count"] >= MIN_BAND_N
                and out[bt]["ev_negative_excluded"]["bet_count"] >= MIN_BAND_N
                and pr is not None
                and er is not None
            ):
                if pr < er:
                    out[bt]["warning"] = "EVプラス群のROIがEVマイナス除外群より低い。EV判定ロジックに重大な問題の可能性"
                else:
                    out[bt]["warning"] = None
            else:
                out[bt]["warning"] = "サンプル不足のため比較結論は出さない"
        return out

    # reasonカテゴリ別
    by_category: Dict[str, List[dict]] = defaultdict(list)
    for r in skip_rows:
        by_category[r.get("skip_category") or "理由未記録"].append(r)

    stage_table = []
    # H: 最終購入
    stage_table.append({"stage": "H_最終購入", "definition": "Purchase確定分", **_agg_rows(purchased_rows)})
    # 各除外カテゴリを「もし買っていたら」
    for cat, rows in sorted(by_category.items(), key=lambda x: -len(x[1])):
        stage_table.append({
            "stage": f"除外:{cat}",
            "definition": "SkippedBetを仮想購入",
            **_agg_rows(rows),
        })
    # 累積近似: 購入 + 特定カテゴリ以外の除外を足していくイメージは複雑なので
    # v1はカテゴリ並列比較。v2再実行は別エンドポイント方針だが、ここでは
    # 「購入∪(EVマイナス以外の除外)」なども出す
    non_ev_neg_skips = [r for r in skip_rows if r not in ev_neg]
    stage_table.append({
        "stage": "C近似_EVフィルタ通過相当",
        "definition": "購入 + EVマイナス以外の見送り",
        **_agg_rows(purchased_rows + non_ev_neg_skips),
    })
    stage_table.append({
        "stage": "A近似_候補全体",
        "definition": "購入 + 全見送り(結果確定分)",
        **_agg_rows(purchased_rows + skip_rows),
    })

    overall_purchased = _agg_rows(purchased_rows)
    overall_ev_neg = _agg_rows(ev_neg)
    warning = None
    if (
        overall_purchased["bet_count"] >= MIN_BAND_N
        and overall_ev_neg["bet_count"] >= MIN_BAND_N
        and overall_purchased.get("actual_roi_pct") is not None
        and overall_ev_neg.get("actual_roi_pct") is not None
        and overall_purchased["actual_roi_pct"] < overall_ev_neg["actual_roi_pct"]
    ):
        warning = "全体でもEV通過群ROI < EVマイナス除外群ROI。EV判定の再検証が必要"

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "overall": {
            "purchased": overall_purchased,
            "ev_negative_excluded": overall_ev_neg,
            "warning": warning,
        },
        "by_bet_type": by_type_compare(purchased_rows, ev_neg),
        "stages_approximate": stage_table,
        "note": (
            "見送りの仮想投資額は同一券種の購入平均stake(無ければ100円)。"
            "actual_payoutがnullの的中は払戻0扱い(ROIは控えめ)。"
            "段階はreasonカテゴリ近似(v1)。race-plan完全再実行は /filter-stages-replay を参照。"
        ),
    }


@router.get("/odds-drift")
def diagnostics_odds_drift(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題5: 購入時オッズとfinal_odds / 実現倍率の乖離。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)

    drifts = []
    missing_final = 0
    for p in purchases:
        at = p.odds_at_purchase
        if at is None or at <= 0:
            continue
        final = p.final_odds
        realized = None
        if p.result == "win" and p.stake_amount and p.payout_amount and p.stake_amount > 0:
            realized = p.payout_amount / p.stake_amount
        ref = final if final is not None and final > 0 else realized
        if ref is None:
            missing_final += 1
            continue
        drifts.append({
            "race_id": p.race_id,
            "bet_type": p.bet_type,
            "ev_pct": p.ev_pct_at_purchase,
            "odds_at_purchase": at,
            "final_or_realized": ref,
            "drift_pct": 100.0 * (ref - at) / at,
            "won": p.result == "win",
        })

    def avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    by_type: Dict[str, List[float]] = defaultdict(list)
    by_band: Dict[str, List[float]] = defaultdict(list)
    for d in drifts:
        by_type[d["bet_type"]].append(d["drift_pct"])
        by_band[_band_for_ev_pct(d.get("ev_pct"))].append(d["drift_pct"])

    high_ev_drift = avg([d["drift_pct"] for d in drifts if (d.get("ev_pct") or 0) >= 20])
    low_ev_drift = avg([d["drift_pct"] for d in drifts if (d.get("ev_pct") or 0) < 5])

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "compared_count": len(drifts),
        "missing_final_odds_count": missing_final,
        "average_drift_pct": avg([d["drift_pct"] for d in drifts]),
        "by_bet_type": {k: avg(v) for k, v in sorted(by_type.items())},
        "by_ev_band": {k: avg(v) for k, v in sorted(by_band.items())},
        "high_ev_ge_120_avg_drift_pct": high_ev_drift,
        "low_ev_lt_105_avg_drift_pct": low_ev_drift,
        "note": (
            "drift_pct = (final_or_realized - odds_at_purchase) / odds_at_purchase × 100。"
            "負なら購入後にオッズ下落。final_oddsが空の場合は的中時の実現倍率を使用。"
        ),
    }


@router.get("/stage-gate")
def diagnostics_stage_gate(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題9・10: 不調ステージ除外の有無比較と未来情報監査メモ。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)
    stake_map = _default_stakes_by_type(purchases)

    purchased_rows = [_purchase_row(p) for p in purchases]
    stage_skips = [
        _skip_row(s, stake_map.get(s.bet_type, 100.0))
        for s in skips
        if (s.reason or "").startswith("不調ステージ除外(")
        or (s.reason or "").startswith("このステージの検証データ不足(")
    ]

    with_gate = _agg_rows(purchased_rows)
    without_gate = _agg_rows(purchased_rows + stage_skips)

    # コード監査メモ(静的)
    lookback_notes = [
        {
            "check": "get_stage_expectancy_map",
            "status": "needs_review",
            "detail": (
                "purchases_router.get_stage_expectancy_mapは確定済み全Purchaseを集計しており、"
                "レース日as-ofカットが無い。過去レースのプラン再計算時に未来の実績が混入しうる。"
            ),
        },
        {
            "check": "stage_sample_n in race-plan",
            "status": "needs_review",
            "detail": (
                "同race_stageのactual_result IS NOT NULL件数を全期間countしており、"
                "当該レース日より後の確定分も含まれうる。"
            ),
        },
    ]

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "with_stage_gate": with_gate,
        "without_stage_gate_virtual": without_gate,
        "stage_related_skip_count": len(stage_skips),
        "lookback_audit": lookback_notes,
        "note": "withoutは不調ステージ/サンプル不足で見送った分を仮想購入に足したROI。",
    }


@router.get("/bet-type-funnel")
def diagnostics_bet_type_funnel(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題12: 購入不足 vs 収益性不足の分離。"""
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)
    stake_map = _default_stakes_by_type(purchases)

    by_type: Dict[str, dict] = {}
    all_types = sorted(set([p.bet_type for p in purchases] + [s.bet_type for s in skips]))
    for bt in all_types:
        ps = [p for p in purchases if p.bet_type == bt]
        ss = [s for s in skips if s.bet_type == bt]
        rows_p = [_purchase_row(p) for p in ps]
        hit_skips = [s for s in ss if s.actual_result == "win"]
        filter_hits = len(hit_skips)
        agg = _agg_rows(rows_p)
        n_purchase = len(ps)
        labels = []
        if n_purchase < MIN_PURCHASE_FOR_PROFIT_JUDGE:
            labels.append("D_サンプル不足")
        if filter_hits > 0:
            labels.append("C_フィルタで的中候補除外あり")
        # 候補生成漏れは既存診断の件数を参照させる
        if n_purchase >= MIN_PURCHASE_FOR_PROFIT_JUDGE and agg.get("actual_roi_pct") is not None:
            if agg["actual_roi_pct"] < 100:
                labels.append("収益性不足(購入集合のROI<100)")
            else:
                labels.append("購入集合はROI>=100")
        elif n_purchase < MIN_PURCHASE_FOR_PROFIT_JUDGE:
            labels.append("ROI断定禁止")

        reason_counts: Dict[str, int] = defaultdict(int)
        for s in ss:
            reason_counts[purchases_router._categorize_skip_reason(s.reason or "")] += 1

        by_type[bt] = {
            "purchase_count": n_purchase,
            "skip_count": len(ss),
            "winning_skips": filter_hits,
            "purchased_stats": agg,
            "labels": labels,
            "skip_reason_categories": dict(reason_counts),
        }

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "by_bet_type": by_type,
        "note": (
            "A候補生成漏れ・Bランキングは /purchases/bet-type-diagnostics の"
            "candidate_generation_miss / ranking を併読。"
            "odds_unavailableは候補生成問題に含めないこと。"
        ),
    }


@router.get("/summary")
def diagnostics_summary(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """主要診断を1発で返すハブ。"""
    return {
        "ev_bands": diagnostics_ev_bands(since=since, db=db),
        "raw_vs_calibrated": diagnostics_raw_vs_calibrated(since=since, db=db),
        "filter_effectiveness": diagnostics_filter_effectiveness(since=since, db=db),
        "odds_drift": diagnostics_odds_drift(since=since, db=db),
        "stage_gate": diagnostics_stage_gate(since=since, db=db),
        "bet_type_funnel": diagnostics_bet_type_funnel(since=since, db=db),
        "reuse_note": (
            "過去サンプルの再利用: 既存Race/Entry/Oddsに対して"
            "race-plan再実行→confirm-resultし直せば、Purchase/Skippedを"
            "現行ロジックで作り直せる。その後本診断を再実行すればよい。"
            "ただしステージ実績ゲートに未来情報が混入している場合は"
            "as-of修正後に再実行すること。"
        ),
    }
