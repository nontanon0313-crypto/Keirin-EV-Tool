"""
収支マイナス原因の切り分け用診断API。

方針:
- 予想モデル・候補生成は変更しない
- Purchase / SkippedBet の既存記録から「確率→EV→購入→実績」を分離計測する
- 閾値やフィルタの自動最適化は行わない(読み取り専用)

期間: since=calibration_switch (既定) または ISO 日時
"""
from __future__ import annotations

import math
import itertools
from collections import defaultdict
from datetime import datetime
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

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

# 項目1: オッズ上限感度分析で検証する仮想上限。Noneは「上限なし」。
ODDS_CAPS = [None, 50.0, 100.0, 200.0, 300.0, 500.0, 1000.0]


def _since_dt(since: Optional[str]) -> Optional[datetime]:
    return purchases_router._parse_since_param(since or "calibration_switch")


def _filter_by_created(q, model, since_dt: Optional[datetime]):
    if since_dt is not None and hasattr(model, "created_at"):
        return q.filter(model.created_at >= since_dt)
    return q


def _load_settled_purchases(db: Session, since_dt: Optional[datetime]) -> List[models.Purchase]:
    # Purchaseにrace relationshipは無い。日付はpurchased_atを使う。
    q = db.query(models.Purchase).filter(models.Purchase.result.in_(("win", "lose"))).filter(models.Purchase.bet_type == "3連単")
    if since_dt is not None:
        q = q.filter(models.Purchase.purchased_at >= since_dt)
    return q.all()


def _race_ids_for_since(db: Session, since_dt: Optional[datetime]) -> Optional[set]:
    """since 以降の診断対象 race_id。None なら全件。"""
    if since_dt is None:
        return None
    ids = set()
    for r in db.query(models.Race.id).filter(
        (models.Race.race_date >= since_dt) | (models.Race.race_date.is_(None))
    ).all():
        ids.add(r[0])
    for r in db.query(models.Purchase.race_id).filter(
        models.Purchase.purchased_at >= since_dt
    ).distinct().all():
        ids.add(r[0])
    return ids


def _load_settled_skips(db: Session, since_dt: Optional[datetime]) -> List[models.SkippedBet]:
    """結果が埋まった見送りのみ（ROI計算用）。購入と同じ race 集合で絞る。"""
    q = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.in_(("win", "lose")))
    )
    ids = _race_ids_for_since(db, since_dt)
    if ids is not None:
        if not ids:
            return []
        q = q.filter(models.SkippedBet.race_id.in_(list(ids)))
    return q.all()


def _load_all_skips(db: Session, since_dt: Optional[datetime]) -> List[models.SkippedBet]:
    """結果の有無を問わず見送り全件。"""
    q = db.query(models.SkippedBet)
    ids = _race_ids_for_since(db, since_dt)
    if ids is not None:
        if not ids:
            return []
        q = q.filter(models.SkippedBet.race_id.in_(list(ids)))
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


def _skip_row(s: models.SkippedBet, default_stake: float = 100.0) -> dict:
    """見送りの仮想ROI用。

    confirm-result / backfill は的中時 actual_payout = 100円 × オッズ で保存している。
    そのため仮想stakeは常に100円に固定しないとROIが崩壊する。
    default_stake引数は互換のため残すが使用しない。
    """
    won = s.actual_result == "win"
    stake = 100.0
    if s.actual_payout is not None:
        payout = float(s.actual_payout)
    else:
        payout = 0.0
    odds = None
    if won and payout > 0:
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


def _effective_odds(p: models.Purchase) -> Optional[float]:
    """購入時オッズが欠けている古い行は、的中時の払戻/投資額から逆算する。"""
    odds = p.odds_at_purchase
    if odds is None and p.stake_amount and p.result == "win" and p.payout_amount:
        odds = p.payout_amount / p.stake_amount
    return float(odds) if odds is not None else None


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


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
            "見送りの仮想投資額は常に100円(actual_payoutが100×オッズ前提)。"
            "actual_payoutがnullの的中は払戻0扱い(ROIは控えめ)。"
            "段階はreasonカテゴリ近似(v1)。race-plan完全再実行は /filter-stages-replay を参照。"
        ),
    }


@router.get("/odds-drift")
def diagnostics_odds_drift(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """課題5: 購入時オッズとfinal_odds / 実現倍率の乖離。

    2026-09-05: 本ツールのオッズは結果確定後に1回だけスクレイピングして保存する
    設計(PROGRESS.md参照)のため、odds_at_purchaseとfinal_odds/実現倍率は
    常に同一のOddsスナップショット由来になり、drift_pctは構造的に常に0になる。
    「投票締切前オッズ→最終オッズ」の変動という意味での実測ドリフトはこの
    データでは検出できない(バグではなく、リアルタイム投票をしていないための
    データモデル上の限界)。
    """
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
            "本ツールはオッズを結果確定後に1回だけ取得するため、この値は構造的に"
            "常に0になる(締切前後の実際のオッズ変動は測定できない)。"
        ),
    }


@router.get("/gate-expectancy-raw")
def diagnostics_gate_expectancy_raw(db: Session = Depends(get_db)):
    """
    実績ゲート(ステージゲート・券種ゲート)が実際に使っている
    get_stage_expectancy_map / get_bet_type_expectancy_map の生値をそのまま返す。

    2026-09-05: 直近24時間replayで2車単がactROI 15.7%(predROI 517.6%)という
    壊滅的な実績にもかかわらず券種ゲートに引っかからなかった件を確認するため追加。
    両関数とも確定済みPurchase全件(as-ofカット無し・全期間)を集計しているため、
    古い購入が多いと直近の悪化が薄まって0%カットラインを超えたままになりうる
    (4.2の未来リーク問題と同根)。値を直接見て確認する。
    """
    stage_exp = purchases_router.get_stage_expectancy_map(db, min_samples=50, use_cache=False)
    bet_type_exp = purchases_router.get_bet_type_expectancy_map(db, min_samples=50, use_cache=False)
    return {
        "stage_expectancy_map": stage_exp,
        "bet_type_expectancy_map": bet_type_exp,
        "gate_cutoffs": {
            "stage_expectancy_cutoff_pct": -50.0,
            "bet_type_expectancy_cutoff_pct": 0.0,
        },
        "note": (
            "どちらの集計もconfirmed Purchase全件(全期間・as-ofカット無し)を対象にしており、"
            "直近窓だけの悪化ではゲートが反応しない可能性がある。"
            "bet_type_expectancy_mapに該当券種が無い場合はサンプル50件未満(全期間)で"
            "ゲート対象外という意味。"
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
    skips_settled = _load_settled_skips(db, since_dt)
    skips_all = _load_all_skips(db, since_dt)
    stake_map = _default_stakes_by_type(purchases)

    by_type: Dict[str, dict] = {}
    all_types = ["3連単"] if (
        any(p.bet_type == "3連単" for p in purchases)
        or any(s.bet_type == "3連単" for s in skips_all)
    ) else []
    for bt in all_types:
        ps = [p for p in purchases if p.bet_type == bt]
        ss = [s for s in skips_all if s.bet_type == bt]
        ss_settled = [s for s in skips_settled if s.bet_type == bt]
        rows_p = [_purchase_row(p) for p in ps]
        hit_skips = [s for s in ss_settled if s.actual_result == "win"]
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



@router.get("/prob-calibration-grid")
def diagnostics_prob_calibration_grid(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    旧券種×勝率帯診断。現行の投票判断・集計対象外。
    購入＋結果付き見送りの両方を使う（除外的中の見落としを含む）。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)

    bands = [
        ("0-5%", 0.0, 0.05),
        ("5-15%", 0.05, 0.15),
        ("15-30%", 0.15, 0.30),
        ("30%以上", 0.30, 1.01),
    ]

    def band_of(prob: Optional[float]) -> Optional[str]:
        if prob is None:
            return None
        for name, lo, hi in bands:
            if lo <= prob < hi:
                return name
        return None

    # rows: (bet_type, band, prob, hit)
    rows = []
    for p in purchases:
        prob = p.win_prob_at_purchase
        if prob is None:
            continue
        b = band_of(float(prob))
        if not b:
            continue
        rows.append((p.bet_type, b, float(prob), 1 if p.result == "win" else 0, "purchase"))
    for s in skips:
        prob = s.win_prob_estimated
        if prob is None:
            continue
        b = band_of(float(prob))
        if not b:
            continue
        rows.append((s.bet_type, b, float(prob), 1 if s.actual_result == "win" else 0, "skip"))

    def agg(items):
        n = len(items)
        if n == 0:
            return {
                "n": 0,
                "hits": 0,
                "actual_hit_rate_pct": None,
                "predicted_avg_pct": None,
                "gap_pt": None,
            }
        hits = sum(x[3] for x in items)
        pred = sum(x[2] for x in items) / n * 100
        actual = hits / n * 100
        return {
            "n": n,
            "hits": hits,
            "actual_hit_rate_pct": round(actual, 4),
            "predicted_avg_pct": round(pred, 4),
            "gap_pt": round(actual - pred, 4),  # 負=予測が楽観
        }

    overall = []
    for name, _, _ in bands:
        items = [r for r in rows if r[1] == name]
        overall.append({"band": name, **agg(items)})

    by_bt = {}
    for bt in sorted({r[0] for r in rows}):
        by_bt[bt] = []
        for name, _, _ in bands:
            items = [r for r in rows if r[0] == bt and r[1] == name]
            by_bt[bt].append({"band": name, **agg(items)})

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "total_rows": len(rows),
        "overall": overall,
        "by_bet_type": by_bt,
        "note": (
            "gap_pt = 実績的中率% - 予測平均%。負なら予測が楽観的。"
            "購入＋結果付き見送りを合算。"
        ),
    }


@router.get("/ev-band-detail")
def diagnostics_ev_band_detail(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    購入のEV帯比較に加え、見送りを仮想100円で同じ帯に載せたときのROIも出す。
    中EV(120-150) vs 超高EV(150+) の差を見る。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)

    def band_name(ev: Optional[float]) -> str:
        if ev is None:
            return "unknown"
        if ev < 0:
            return "EVマイナス"
        if ev < 5:
            return "0-5%(回収100-105)"
        if ev < 10:
            return "5-10%"
        if ev < 20:
            return "10-20%"
        if ev < 50:
            return "20-50%(帯120-150相当)"
        return "50%以上(帯150+相当)"

    # purchases: real stake
    p_rows = []
    for p in purchases:
        r = _purchase_row(p)
        r["band"] = band_name(r.get("ev_pct"))
        p_rows.append(r)

    # skips: virtual 100
    s_rows = []
    for s in skips:
        r = _skip_row(s, 100.0)
        r["band"] = band_name(r.get("ev_pct"))
        s_rows.append(r)

    band_order = [
        "EVマイナス",
        "0-5%(回収100-105)",
        "5-10%",
        "10-20%",
        "20-50%(帯120-150相当)",
        "50%以上(帯150+相当)",
        "unknown",
    ]

    def summarize(rows, stake_mode: str):
        out = []
        for b in band_order:
            sub = [x for x in rows if x.get("band") == b]
            if not sub:
                continue
            st = _agg_rows(sub)
            st["band"] = b
            st["stake_mode"] = stake_mode
            out.append(st)
        return out

    # focus: mid vs high among purchases only (same as before but clearer labels)
    mid = [r for r in p_rows if r.get("band") == "20-50%(帯120-150相当)"]
    high = [r for r in p_rows if r.get("band") == "50%以上(帯150+相当)"]

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "purchases_by_band": summarize(p_rows, "real_stake"),
        "skips_by_band": summarize(s_rows, "virtual_100"),
        "focus": {
            "purchase_mid_ev_120_150_equiv": _agg_rows(mid) if mid else None,
            "purchase_high_ev_150_plus_equiv": _agg_rows(high) if high else None,
            "note": (
                "ev_pct 20-50 ≒ 予測回収120-150%、ev_pct>=50 ≒ 150%以上。"
                "購入は実stake、見送りは仮想100円。"
            ),
        },
    }


@router.get("/discarded-hits")
def diagnostics_discarded_hits(
    since: Optional[str] = Query("calibration_switch"),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    見送りのうち的中したもの（捨てた当たり）の分布。
    EV・勝率・理由を見て「何を落としているか」を確認する。
    """
    since_dt = _since_dt(since)
    skips = _load_settled_skips(db, since_dt)
    hits = [s for s in skips if s.actual_result == "win"]

    def ev_bucket(ev: Optional[float]) -> str:
        if ev is None:
            return "unknown"
        if ev < 0:
            return "EVマイナス"
        if ev < 20:
            return "EV0-20"
        if ev < 50:
            return "EV20-50"
        return "EV50+"

    def prob_bucket(p: Optional[float]) -> str:
        if p is None:
            return "unknown"
        if p < 0.05:
            return "0-5%"
        if p < 0.15:
            return "5-15%"
        if p < 0.30:
            return "15-30%"
        return "30%+"

    by_ev = Counter()
    by_prob = Counter()
    by_reason = Counter()
    by_bt = Counter()
    payout_sum = 0.0
    for s in hits:
        by_ev[ev_bucket(s.ev_pct_estimated)] += 1
        by_prob[prob_bucket(s.win_prob_estimated)] += 1
        by_reason[(s.reason or "なし")[:50]] += 1
        by_bt[s.bet_type] += 1
        if s.actual_payout:
            payout_sum += float(s.actual_payout)

    # top hits by payout
    ranked = sorted(
        hits,
        key=lambda s: float(s.actual_payout or 0),
        reverse=True,
    )[:limit]
    samples = []
    for s in ranked:
        samples.append({
            "race_id": s.race_id,
            "bet_type": s.bet_type,
            "combination": s.combination,
            "ev_pct": s.ev_pct_estimated,
            "win_prob": s.win_prob_estimated,
            "actual_payout_per_100": s.actual_payout,
            "reason": (s.reason or "")[:80],
        })

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "discarded_hit_count": len(hits),
        "virtual_return_total_per_100yen": round(payout_sum, 2),
        "by_bet_type": dict(by_bt),
        "by_ev_bucket": dict(by_ev),
        "by_prob_bucket": dict(by_prob),
        "top_reasons": by_reason.most_common(15),
        "top_hits_by_payout": samples,
        "note": (
            "見送りで的中した買い目。仮想投資100円あたりの払戻合計は"
            "virtual_return_total_per_100yen。"
        ),
    }




@router.get("/mid-vs-high-ev")
def diagnostics_mid_vs_high_ev(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    ① 購入を中EV帯だけに限定した場合 vs 高EV帯だけの実績ROI。
    実stakeのまま比較（仮想ではない）。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    rows = [_purchase_row(p) for p in purchases]

    def is_mid(ev):
        return ev is not None and 20.0 <= ev < 50.0

    def is_high(ev):
        return ev is not None and ev >= 50.0

    mid = [r for r in rows if is_mid(r.get("ev_pct"))]
    high = [r for r in rows if is_high(r.get("ev_pct"))]
    other = [r for r in rows if r.get("ev_pct") is not None and not is_mid(r.get("ev_pct")) and not is_high(r.get("ev_pct"))]
    all_pos = [r for r in rows if r.get("ev_pct") is not None and r.get("ev_pct") >= 0]

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "all_purchases": _agg_rows(rows),
        "mid_ev_only_20_to_50": _agg_rows(mid),
        "high_ev_only_50_plus": _agg_rows(high),
        "other_ev": _agg_rows(other),
        "all_nonneg_ev": _agg_rows(all_pos),
        "note": (
            "mid=ev_pct 20〜50（予測回収120〜150%相当）、"
            "high=ev_pct≥50（150%以上相当）。実stake。"
            "midのROIが高より良ければ、高EV優先が逆効果。"
        ),
    }


@router.get("/stage-gate-off-virtual")
def diagnostics_stage_gate_off_virtual(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    ② ステージ不足で落とした着順券（3連単・2車単）を仮想100円で戻した場合のROI。
    look-ahead注意: ステージ判定自体に未来実績が混ざる可能性あり（既存stage-gate監査参照）。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)

    def is_stage_skip(s: models.SkippedBet) -> bool:
        r = s.reason or ""
        return "ステージ" in r and ("不足" in r or "検証データ" in r)

    stage_skips = [s for s in skips if is_stage_skip(s)]
    stage_rows = [_skip_row(s, 100.0) for s in stage_skips]

    by_bt: Dict[str, list] = defaultdict(list)
    for r in stage_rows:
        by_bt[r.get("bet_type") or "?"].append(r)

    # 購入実績 + ステージ落としを足した「ゲート無し仮想」
    # 購入は実stakeのまま、ステージ落としだけ仮想100円 → 混在するので
    # 比較用に「ステージ落としのみ」と「購入のみ」を分けて返す
    purchase_rows = [_purchase_row(p) for p in purchases]
    order_purchases = [r for r in purchase_rows if r.get("bet_type") in ("3連単", "2車単")]

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "stage_gated_skips_virtual_100": _agg_rows(stage_rows),
        "by_bet_type": {bt: _agg_rows(rs) for bt, rs in sorted(by_bt.items())},
        "order_sensitive_purchases_actual": _agg_rows(order_purchases),
        "discarded_hit_count": sum(1 for s in stage_skips if s.actual_result == "win"),
        "stage_skip_count": len(stage_skips),
        "note": (
            "ステージ不足理由の見送りを仮想100円で評価。"
            "的中が多く払戻が乗れば、ゲートが大穴的中を落としている。"
            "ステージ判定のlook-aheadバイアスは /stage-gate の監査を併読。"
        ),
    }


@router.get("/ev-negative-recheck")
def diagnostics_ev_negative_recheck(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    ③ 「期待値マイナス」で見送った的中について、保存オッズ×保存確率でEVを再計算。
    本当に負EVだったか、記録・計算の不整合かを見る。
    """
    since_dt = _since_dt(since)
    skips = _load_settled_skips(db, since_dt)

    def is_ev_neg_reason(s: models.SkippedBet) -> bool:
        r = s.reason or ""
        return "期待値マイナス" in r

    targets = [s for s in skips if is_ev_neg_reason(s)]
    hits = [s for s in targets if s.actual_result == "win"]

    # Odds表から倍率を取る
    race_ids = {s.race_id for s in targets}
    odds_map: Dict[Tuple[int, str, str], float] = {}
    if race_ids:
        for o in (
            db.query(models.Odds)
            .filter(models.Odds.race_id.in_(list(race_ids)))
            .all()
        ):
            if o.odds_value and o.odds_value > 0:
                odds_map[(o.race_id, o.bet_type, o.combination)] = float(o.odds_value)

    recheck = []
    buckets = Counter()
    for s in hits:
        prob = s.win_prob_estimated
        stored_ev = s.ev_pct_estimated
        odds = odds_map.get((s.race_id, s.bet_type, s.combination))
        recomputed = None
        if prob is not None and odds is not None and odds > 0:
            recomputed = calc.calc_ev_pct(float(prob), float(odds), 0.0)
        if recomputed is None:
            buckets["recompute_failed"] += 1
            label = "再計算不能"
        elif recomputed < 0:
            buckets["still_negative"] += 1
            label = "再計算でも負"
        elif recomputed < 20:
            buckets["recomputed_low_positive"] += 1
            label = "再計算で弱い正"
        else:
            buckets["recomputed_high_positive"] += 1
            label = "再計算で強い正"
        recheck.append({
            "race_id": s.race_id,
            "bet_type": s.bet_type,
            "combination": s.combination,
            "stored_ev_pct": stored_ev,
            "recomputed_ev_pct": round(recomputed, 4) if recomputed is not None else None,
            "prob": prob,
            "odds": odds,
            "actual_payout_per_100": s.actual_payout,
            "label": label,
        })

    # 払戻上位
    recheck_sorted = sorted(
        recheck,
        key=lambda x: float(x.get("actual_payout_per_100") or 0),
        reverse=True,
    )

    # 全体（的中以外も含む）の再計算分布
    all_labels = Counter()
    for s in targets:
        prob = s.win_prob_estimated
        odds = odds_map.get((s.race_id, s.bet_type, s.combination))
        if prob is None or odds is None or odds <= 0:
            all_labels["recompute_failed"] += 1
            continue
        rev = calc.calc_ev_pct(float(prob), float(odds), 0.0)
        if rev < 0:
            all_labels["still_negative"] += 1
        elif rev < 20:
            all_labels["recomputed_low_positive"] += 1
        else:
            all_labels["recomputed_high_positive"] += 1

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "ev_neg_skip_count": len(targets),
        "ev_neg_hit_count": len(hits),
        "hit_recheck_buckets": dict(buckets),
        "all_ev_neg_recheck_buckets": dict(all_labels),
        "top_hits": recheck_sorted[:25],
        "note": (
            "stored_ev は記録時の値。recomputed は Odds表×win_prob_estimated で再計算。"
            "再計算でも負が多いなら「負EVでも当たる」世界。"
            "再計算で正が多いなら記録時のEV計算・確率に不整合の疑い。"
        ),
    }





@router.get("/race-plan-design")
def diagnostics_race_plan_design():
    """
    投票プランが何を最大化しているかの設計メモ（読み取り専用）。
    コード上の既定値に基づく。
    """
    return {
        "objective": {
            "primary_rank": "候補を ev_pct 降順で走査し、制約を満たすものから採用",
            "stake": "Kelly分数 × fractional_coefficient(既定0.25) × bankroll を1点上限でクリップし100円単位",
            "filter_in": [
                "min_win_prob（既定0.05）",
                "min_ev_pct（既定5.0）",
                "実績ゲート（不調ステージ・不調券種）適用時",
            ],
            "filter_out_or_cap": [
                "max_items（既定20）",
                "max_race_pct（既定10% of bankroll）",
                "avoid_garami（的中しても合計stakeを下回らない安全オッズ判定）",
                "ステージサンプル不足時は3連単・2車単を除外",
            ],
        },
        "what_it_maximizes_in_practice": [
            "予測EVの高い順に枠と予算を埋める",
            "高オッズ×（補正後）確率がKellyとEVの両方を押し上げやすい",
            "結果として購入が超高EV帯に偏りやすい",
        ],
        "what_it_does_not_optimize": [
            "実績ROI",
            "的中率",
            "予測確率の校正誤差を直接罰する指標",
            "オッズ帯の分散（高オッズ集中の抑制）",
        ],
        "note": (
            "これは閾値変更の提案ではなく、現行race-planの目的関数の明示。"
            "代替順位付けの仮想比較は /race-plan-rank-compare を参照。"
        ),
    }


@router.get("/race-plan-rank-compare")
def diagnostics_race_plan_rank_compare(
    since: Optional[str] = Query("calibration_switch"),
    top_k: int = Query(5, ge=1, le=20),
    min_ev_pct: float = Query(5.0, description="候補に残す最低EV%（現行デフォルトに合わせる）"),
    db: Session = Depends(get_db),
):
    """
    同一レース内で「順位付けルールだけ」を変えた仮想比較。
    各レースで条件を満たす候補を並べ、上位top_kを仮想100円で購入した場合のROI。
    ガミり・予算・Kellyは入れない（順位付けの差だけを見る）。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)

    # 候補プール: 購入＋見送り（結果付き）
    # oddsは購入はodds_at_purchase、見送りはOdds表
    race_ids = {p.race_id for p in purchases} | {s.race_id for s in skips}
    odds_map: Dict[Tuple[int, str, str], float] = {}
    if race_ids:
        for o in db.query(models.Odds).filter(models.Odds.race_id.in_(list(race_ids))).all():
            if o.odds_value and o.odds_value > 0:
                odds_map[(o.race_id, o.bet_type, o.combination)] = float(o.odds_value)

    # race_id -> list of candidate dicts
    by_race: Dict[int, List[dict]] = defaultdict(list)
    seen = set()

    def add_cand(race_id, bet_type, combination, prob, odds, won, source, stored_ev=None):
        key = (race_id, bet_type, combination)
        if key in seen or prob is None or odds is None or odds <= 0:
            return
        seen.add(key)
        ev = calc.calc_ev_pct(float(prob), float(odds), 0.0)
        by_race[race_id].append({
            "race_id": race_id,
            "bet_type": bet_type,
            "combination": combination,
            "prob": float(prob),
            "odds": float(odds),
            "ev_pct": float(ev),
            "stored_ev": stored_ev,
            "won": bool(won),
            "source": source,
        })

    for p in purchases:
        odds = p.odds_at_purchase or odds_map.get((p.race_id, p.bet_type, p.combination))
        add_cand(
            p.race_id, p.bet_type, p.combination,
            p.win_prob_at_purchase, odds,
            p.result == "win", "purchase", p.ev_pct_at_purchase,
        )
    for s in skips:
        odds = odds_map.get((s.race_id, s.bet_type, s.combination))
        add_cand(
            s.race_id, s.bet_type, s.combination,
            s.win_prob_estimated, odds,
            s.actual_result == "win", "skip", s.ev_pct_estimated,
        )

    policies = {
        "ev_desc": lambda c: (-c["ev_pct"], -c["prob"]),
        "prob_desc": lambda c: (-c["prob"], -c["ev_pct"]),
        "odds_asc_among_plus_ev": lambda c: (c["odds"], -c["prob"]),
        "ev_desc_cap50": lambda c: (-min(c["ev_pct"], 50.0), -c["prob"]),
        "prob_times_log_odds": lambda c: (
            -(c["prob"] * (0.0 if c["odds"] <= 1 else __import__("math").log(c["odds"]))),
            -c["prob"],
        ),
    }

    def run_policy(name, key_fn):
        picked = []
        races_used = 0
        for rid, cands in by_race.items():
            pool = [c for c in cands if c["ev_pct"] >= min_ev_pct]
            if name == "odds_asc_among_plus_ev":
                pool = [c for c in pool if c["ev_pct"] >= min_ev_pct]
            if not pool:
                continue
            races_used += 1
            ranked = sorted(pool, key=key_fn)
            for c in ranked[:top_k]:
                picked.append({
                    **c,
                    "stake": 100.0,
                    "payout": 100.0 * c["odds"] if c["won"] else 0.0,
                })
        if not picked:
            return {
                "policy": name,
                "bet_count": 0,
                "race_count": 0,
                "hit_count": 0,
                "actual_roi_pct": None,
                "actual_hit_rate_pct": None,
                "avg_odds": None,
                "avg_ev_pct": None,
                "avg_prob": None,
            }
        stake = sum(x["stake"] for x in picked)
        payout = sum(x["payout"] for x in picked)
        hits = sum(1 for x in picked if x["won"])
        return {
            "policy": name,
            "bet_count": len(picked),
            "race_count": races_used,
            "hit_count": hits,
            "actual_roi_pct": round(100.0 * payout / stake, 4) if stake > 0 else None,
            "actual_hit_rate_pct": round(100.0 * hits / len(picked), 4),
            "avg_odds": round(sum(x["odds"] for x in picked) / len(picked), 4),
            "avg_ev_pct": round(sum(x["ev_pct"] for x in picked) / len(picked), 4),
            "avg_prob": round(sum(x["prob"] for x in picked) / len(picked), 4),
        }

    results = [run_policy(n, fn) for n, fn in policies.items()]
    results_sorted = sorted(
        results,
        key=lambda x: (x["actual_roi_pct"] is not None, x["actual_roi_pct"] or 0),
        reverse=True,
    )

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "top_k": top_k,
        "min_ev_pct": min_ev_pct,
        "race_count_with_pool": sum(1 for cs in by_race.values() if any(c["ev_pct"] >= min_ev_pct for c in cs)),
        "policies": results_sorted,
        "policy_meanings": {
            "ev_desc": "現行に近い: 予測EV高い順",
            "prob_desc": "的中確率高い順（本命寄り）",
            "odds_asc_among_plus_ev": "EV条件を満たす中で低オッズ優先",
            "ev_desc_cap50": "EVを50%で頭打ちしてから高い順（超高EVの優先を弱める）",
            "prob_times_log_odds": "確率×log(オッズ)（極端オッズの影響を抑えた折衷）",
        },
        "note": (
            "同一レース・同一候補プール・仮想100円・上位top_kのみ。"
            "ガミり・予算・Kellyは除外（順位付けの差だけ）。"
            "ROIが高くても本番採用前に別期間で再確認すること。"
            "これは閾値の本番変更ではない。"
        ),
    }





@router.get("/stable-wide-strategies")
def diagnostics_stable_wide_strategies(
    since: Optional[str] = Query("calibration_switch"),
    top_k: int = Query(3, ge=1, le=10, description="1レースあたりの最大点数"),
    db: Session = Depends(get_db),
):
    """
    ワイド中心・的中率寄りの選び方で仮想ROIが100%を超えるかを測る。
    各レースで条件を満たす候補だけを並べ、上位top_kを仮想100円。
    本番閾値は変更しない（読み取り専用）。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    skips = _load_settled_skips(db, since_dt)

    race_ids = {p.race_id for p in purchases} | {s.race_id for s in skips}
    odds_map: Dict[Tuple[int, str, str], float] = {}
    if race_ids:
        for o in db.query(models.Odds).filter(models.Odds.race_id.in_(list(race_ids))).all():
            if o.odds_value and o.odds_value > 0:
                odds_map[(o.race_id, o.bet_type, o.combination)] = float(o.odds_value)

    by_race: Dict[int, List[dict]] = defaultdict(list)
    seen = set()

    def add(race_id, bet_type, combination, prob, odds, won):
        key = (race_id, bet_type, combination)
        if key in seen or prob is None or odds is None or odds <= 0:
            return
        if bet_type != "ワイド":
            return
        seen.add(key)
        ev = calc.calc_ev_pct(float(prob), float(odds), 0.0)
        by_race[race_id].append({
            "race_id": race_id,
            "bet_type": bet_type,
            "combination": combination,
            "prob": float(prob),
            "odds": float(odds),
            "ev_pct": float(ev),
            "won": bool(won),
        })

    for p in purchases:
        odds = p.odds_at_purchase or odds_map.get((p.race_id, p.bet_type, p.combination))
        add(p.race_id, p.bet_type, p.combination, p.win_prob_at_purchase, odds, p.result == "win")
    for s in skips:
        odds = odds_map.get((s.race_id, s.bet_type, s.combination))
        add(s.race_id, s.bet_type, s.combination, s.win_prob_estimated, odds, s.actual_result == "win")

    # 戦略定義: (id, min_prob, max_prob, min_odds, max_odds, min_ev, rank_key)
    import math
    strategies = []

    def rank_prob(c):
        return (-c["prob"], c["odds"])

    def rank_odds_asc(c):
        return (c["odds"], -c["prob"])

    def rank_ev(c):
        return (-c["ev_pct"], -c["prob"])

    def rank_prob_log_odds(c):
        lo = math.log(c["odds"]) if c["odds"] > 1 else 0.0
        return (-(c["prob"] * lo), -c["prob"])

    # 本命寄りグリッド
    for min_p, max_p, min_o, max_o, min_ev, rank_name, rank_fn in [
        (0.15, 1.01, 1.0, 15.0, -100.0, "prob_desc", rank_prob),  # 高確率・低〜中オッズ（EV条件なし）
        (0.15, 1.01, 1.0, 15.0, 0.0, "prob_desc", rank_prob),
        (0.15, 1.01, 1.0, 15.0, 5.0, "prob_desc", rank_prob),
        (0.10, 0.40, 1.0, 20.0, 0.0, "prob_desc", rank_prob),
        (0.10, 0.40, 1.0, 20.0, 5.0, "prob_desc", rank_prob),
        (0.15, 1.01, 1.0, 10.0, 0.0, "odds_asc", rank_odds_asc),
        (0.15, 1.01, 1.0, 10.0, 5.0, "odds_asc", rank_odds_asc),
        (0.20, 1.01, 1.0, 8.0, 0.0, "prob_desc", rank_prob),
        (0.20, 1.01, 1.0, 8.0, 5.0, "prob_desc", rank_prob),
        (0.15, 1.01, 1.0, 15.0, 5.0, "ev_desc", rank_ev),
        (0.10, 1.01, 1.0, 25.0, 5.0, "prob_log_odds", rank_prob_log_odds),
        # 参考: 現行に近い（ワイドのみ・EV順・緩い）
        (0.0, 1.01, 1.0, 9999.0, 5.0, "ev_desc", rank_ev),
        (0.05, 1.01, 1.0, 9999.0, 5.0, "ev_desc", rank_ev),
    ]:
        strategies.append({
            "min_prob": min_p,
            "max_prob": max_p,
            "min_odds": min_o,
            "max_odds": max_o,
            "min_ev_pct": min_ev,
            "rank": rank_name,
            "rank_fn": rank_fn,
        })

    def run(st):
        picked = []
        races_used = 0
        for rid, cands in by_race.items():
            pool = []
            for c in cands:
                if not (st["min_prob"] <= c["prob"] < st["max_prob"]):
                    continue
                if not (st["min_odds"] <= c["odds"] <= st["max_odds"]):
                    continue
                if c["ev_pct"] < st["min_ev_pct"]:
                    continue
                pool.append(c)
            if not pool:
                continue
            races_used += 1
            ranked = sorted(pool, key=st["rank_fn"])
            for c in ranked[:top_k]:
                picked.append({
                    **c,
                    "stake": 100.0,
                    "payout": 100.0 * c["odds"] if c["won"] else 0.0,
                })
        if not picked:
            return None
        stake = sum(x["stake"] for x in picked)
        payout = sum(x["payout"] for x in picked)
        hits = sum(1 for x in picked if x["won"])
        roi = 100.0 * payout / stake if stake > 0 else None
        return {
            "min_prob": st["min_prob"],
            "max_prob": st["max_prob"],
            "min_odds": st["min_odds"],
            "max_odds": st["max_odds"],
            "min_ev_pct": st["min_ev_pct"],
            "rank": st["rank"],
            "bet_count": len(picked),
            "race_count": races_used,
            "hit_count": hits,
            "actual_hit_rate_pct": round(100.0 * hits / len(picked), 4),
            "actual_roi_pct": round(roi, 4) if roi is not None else None,
            "avg_odds": round(sum(x["odds"] for x in picked) / len(picked), 4),
            "avg_prob": round(sum(x["prob"] for x in picked) / len(picked), 4),
            "avg_ev_pct": round(sum(x["ev_pct"] for x in picked) / len(picked), 4),
            "meets_breakeven": bool(roi is not None and roi >= 100.0),
        }

    results = []
    for st in strategies:
        r = run(st)
        if r:
            results.append(r)

    results_sorted = sorted(
        results,
        key=lambda x: (x.get("meets_breakeven") is True, x.get("actual_roi_pct") or 0),
        reverse=True,
    )
    profitable = [r for r in results_sorted if r.get("meets_breakeven")]

    # 実購入ワイドの参考
    wide_purchases = [_purchase_row(p) for p in purchases if p.bet_type == "ワイド"]
    actual_wide = _agg_rows(wide_purchases) if wide_purchases else None

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "top_k_per_race": top_k,
        "bet_type": "ワイド",
        "actual_wide_purchases": actual_wide,
        "strategies_tested": len(results_sorted),
        "strategies_meeting_breakeven": len(profitable),
        "best_strategies": results_sorted[:12],
        "profitable_strategies": profitable[:12],
        "note": (
            "ワイドのみ・仮想100円・1レースtop_k点。"
            "meets_breakeven=ROI>=100%。"
            "1期間の結果なので、黒字戦略があっても別期間再確認が必要。"
            "本番閾値は変更していない。"
        ),
    }





@router.get("/winning-capture")
def diagnostics_winning_capture(
    since: Optional[str] = Query("all"),
    limit_races: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """
    確定レースで「実際に的中した買い目」が
    purchase / skipped / not_recorded のどれに落ちたかを集計する。
    replay後の winning_diagnostics と同じ判定をバッチで行う。
    """
    from .. import ev_calculator as calc

    since_dt = _since_dt(since)
    q = db.query(models.Race).filter(models.Race.actual_result.isnot(None))
    if since_dt is not None:
        # purchased_at 基準ではなく race_date（開催日）
        q = q.filter(models.Race.race_date >= since_dt)
    races = q.order_by(models.Race.id.desc()).limit(limit_races).all()

    status_counts = Counter()
    reason_counts = Counter()
    by_bet_type = defaultdict(Counter)
    samples_not_recorded = []
    samples_skipped = []
    races_used = 0

    for race in races:
        try:
            actual = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue
        races_used += 1
        purchases_by_key = {
            (p.bet_type, p.combination): p
            for p in db.query(models.Purchase).filter(models.Purchase.race_id == race.id).all()
        }
        skipped_by_key = {
            (s.bet_type, s.combination): s
            for s in db.query(models.SkippedBet).filter(models.SkippedBet.race_id == race.id).all()
        }
        for o in db.query(models.Odds).filter(models.Odds.race_id == race.id).all():
            if o.bet_type not in ("2車単", "2車複", "3連単", "3連複", "ワイド"):
                continue
            if not calc.judge_purchase_result(o.bet_type, o.combination, actual):
                continue
            key = (o.bet_type, o.combination)
            purchase = purchases_by_key.get(key)
            skipped = skipped_by_key.get(key)
            if purchase is not None:
                status = "purchase"
                reason = None
            elif skipped is not None:
                status = "skipped"
                reason = skipped.reason or ""
            else:
                status = "not_recorded"
                reason = "候補に残らず記録なし"
            status_counts[status] += 1
            by_bet_type[o.bet_type][status] += 1
            if reason:
                # 理由は先頭40文字で集約
                reason_counts[reason[:60]] += 1
            row = {
                "race_id": race.id,
                "bet_type": o.bet_type,
                "combination": o.combination,
                "odds": o.odds_value,
                "status": status,
                "reason": reason,
            }
            if status == "not_recorded" and len(samples_not_recorded) < 15:
                samples_not_recorded.append(row)
            if status == "skipped" and len(samples_skipped) < 15:
                samples_skipped.append(row)

    total = sum(status_counts.values()) or 1
    return {
        "since": since,
        "races_used": races_used,
        "winning_outcomes_total": sum(status_counts.values()),
        "status_counts": dict(status_counts),
        "status_pct": {k: round(100.0 * v / total, 2) for k, v in status_counts.items()},
        "by_bet_type": {bt: dict(c) for bt, c in by_bet_type.items()},
        "top_skip_reasons": reason_counts.most_common(15),
        "samples_not_recorded": samples_not_recorded,
        "samples_skipped": samples_skipped,
        "note": (
            "的中買い目がpurchaseに乗った割合が低い場合、"
            "校正が強すぎる・フィルタ・候補生成漏れを疑う。"
            "not_recordedは評価自体が走っていないか、上位Nの検証記録からも外れたもの。"
        ),
    }



@router.get("/odds-cap-sensitivity")
def diagnostics_odds_cap_sensitivity(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    項目1: オッズ上限感度分析(読み取り専用)。

    実際に確定済みのPurchase(実購入)を対象に、仮想的なオッズ上限
    (上限なし/50/100/200/300/500/1000倍)を設定した場合に実績がどう変化するかを
    再計算する。「高オッズを除外すべき」という結論は出さず、あくまで収益構造が
    オッズ上限によってどう変化するかを見えるようにするだけ。
    購入判定・予想ロジック・EV計算は一切変更しない。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)

    by_cap = []
    for cap in ODDS_CAPS:
        included: List[Tuple[models.Purchase, Optional[float]]] = []
        excluded_unknown_odds = 0
        for p in purchases:
            odds = _effective_odds(p)
            if odds is None:
                if cap is None:
                    included.append((p, odds))
                else:
                    excluded_unknown_odds += 1
                continue
            if cap is None or odds <= cap:
                included.append((p, odds))

        n = len(included)
        if n == 0:
            by_cap.append({
                "odds_cap": cap if cap is not None else "上限なし",
                "bet_count": 0,
                "excluded_unknown_odds": excluded_unknown_odds,
                "note": "該当するベットがありません",
            })
            continue

        stake_sum = sum(float(p.stake_amount or 0) for p, _ in included)
        payout_sum = sum(float(p.payout_amount or 0) for p, _ in included)
        hits = sum(1 for p, _ in included if p.result == "win")
        profit_total = payout_sum - stake_sum
        roi_pct = round(100.0 * payout_sum / stake_sum, 2) if stake_sum > 0 else None

        # レース単位に集約し、時系列順に並べて黒字レース率・最大ドローダウンを出す。
        race_agg: Dict[int, Dict[str, Any]] = {}
        for p, _ in included:
            r = race_agg.setdefault(p.race_id, {"stake": 0.0, "payout": 0.0, "t": p.purchased_at})
            r["stake"] += float(p.stake_amount or 0)
            r["payout"] += float(p.payout_amount or 0)
            if p.purchased_at is not None and (r["t"] is None or p.purchased_at < r["t"]):
                r["t"] = p.purchased_at
        race_rows = sorted(race_agg.values(), key=lambda r: (r["t"] is None, r["t"]))
        race_count = len(race_rows)
        black_race_count = sum(1 for r in race_rows if r["payout"] >= r["stake"])
        black_race_rate_pct = round(100.0 * black_race_count / race_count, 2) if race_count else None

        peak = 0.0
        cum = 0.0
        max_drawdown = 0.0
        for r in race_rows:
            cum += (r["payout"] - r["stake"])
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_drawdown:
                max_drawdown = dd

        # 利益集中度: 的中した買い目1件ごとの純利益を降順に並べ、
        # 上位1/5/10件が「的中による総利益」に占める割合(要求分析の
        # 「上位10的中で利益の64.2%」に対応する指標)。
        hit_profits = sorted(
            (float(p.payout_amount or 0) - float(p.stake_amount or 0) for p, _ in included if p.result == "win"),
            reverse=True,
        )
        total_hit_profit = sum(hit_profits)

        def top_share(k: int) -> Optional[float]:
            if not hit_profits or total_hit_profit <= 0:
                return None
            return round(100.0 * sum(hit_profits[:k]) / total_hit_profit, 2)

        odds_values = [o for _, o in included if o is not None]

        by_cap.append({
            "odds_cap": cap if cap is not None else "上限なし",
            "bet_count": n,
            "excluded_unknown_odds": excluded_unknown_odds,
            "hit_count": hits,
            "hit_rate_pct": round(100.0 * hits / n, 2),
            "stake_total": round(stake_sum, 0),
            "payout_total": round(payout_sum, 0),
            "profit_total": round(profit_total, 0),
            "roi_pct": roi_pct,
            "race_count": race_count,
            "black_race_count": black_race_count,
            "black_race_rate_pct": black_race_rate_pct,
            "max_drawdown": round(max_drawdown, 0),
            "profit_concentration": {
                "top1_hit_profit_share_pct": top_share(1),
                "top5_hit_profit_share_pct": top_share(5),
                "top10_hit_profit_share_pct": top_share(10),
            },
            "odds_median": round(_percentile(odds_values, 50), 2) if odds_values else None,
            "odds_p90": round(_percentile(odds_values, 90), 2) if odds_values else None,
            "odds_max": round(max(odds_values), 2) if odds_values else None,
        })

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "total_settled_bets": len(purchases),
        "odds_caps_tested": [c if c is not None else "上限なし" for c in ODDS_CAPS],
        "by_odds_cap": by_cap,
        "note": (
            "実際に確定済みのPurchase(実購入)のみが対象です。見送り(SkippedBet)は"
            "含みません。仮想的にオッズ上限を適用した場合の再計算であり、この結果を"
            "根拠に自動で高オッズを除外する変更は行っていません(読み取り専用の"
            "感度分析)。上限を下げるほど対象ベット数・レース数が減るため、"
            "件数が極端に少ないキャップの数値は参考程度に留めてください。"
        ),
    }


@router.get("/exclude-top-hits-sensitivity")
def diagnostics_exclude_top_hits_sensitivity(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    ChatGPT分析9項目の項目8: 上位的中を除外した場合の損益(読み取り専用)。

    `odds-cap-sensitivity`が「オッズ上限を下げたら」を見るのに対し、こちらは
    「点数ベースで上位1/5/10件の的中を除いたら、それでも黒字が残るか」を見る。
    実際に確定済みのPurchase(実購入)のみが対象。既存の購入判定・予想ロジックは
    一切変更しない。
    """
    since_dt = _since_dt(since)
    purchases = _load_settled_purchases(db, since_dt)
    if not purchases:
        return {"message": "対象データがありません"}

    stake_total = sum(float(p.stake_amount or 0) for p in purchases)
    payout_total = sum(float(p.payout_amount or 0) for p in purchases)

    hit_purchases_desc = sorted(
        (p for p in purchases if p.result == "win"),
        key=lambda p: float(p.payout_amount or 0) - float(p.stake_amount or 0),
        reverse=True,
    )

    def _after_excluding(k: int) -> Dict[str, Any]:
        excluded = hit_purchases_desc[:k]
        excluded_payout = sum(float(p.payout_amount or 0) for p in excluded)
        remaining_payout = payout_total - excluded_payout
        remaining_profit = remaining_payout - stake_total
        remaining_roi_pct = round(remaining_payout / stake_total * 100, 2) if stake_total > 0 else None
        return {
            "excluded_count": len(excluded),
            "remaining_profit": round(remaining_profit, 0),
            "remaining_roi_pct": remaining_roi_pct,
            "still_profitable": remaining_profit > 0,
        }

    by_odds_threshold = []
    for cap in (100.0, 500.0, 1000.0):
        included_stake = 0.0
        included_payout = 0.0
        for p in purchases:
            odds = p.odds_at_purchase
            if odds is None or odds <= 0:
                odds = p.final_odds
            if odds is not None and odds >= cap:
                continue  # このcap以上の的中(投資分含む)を除外
            included_stake += float(p.stake_amount or 0)
            included_payout += float(p.payout_amount or 0)
        by_odds_threshold.append({
            "exclude_odds_at_or_above": cap,
            "remaining_bet_count": sum(
                1 for p in purchases
                if not ((p.odds_at_purchase or p.final_odds or 0) >= cap)
            ),
            "remaining_profit": round(included_payout - included_stake, 0),
            "remaining_roi_pct": round(included_payout / included_stake * 100, 2) if included_stake > 0 else None,
            "still_profitable": (included_payout - included_stake) > 0,
        })

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "total_bets": len(purchases),
        "actual_profit": round(payout_total - stake_total, 0),
        "actual_roi_pct": round(payout_total / stake_total * 100, 2) if stake_total > 0 else None,
        "exclude_top_n_hits": {
            "top1": _after_excluding(1),
            "top5": _after_excluding(5),
            "top10": _after_excluding(10),
        },
        "exclude_by_odds_threshold": by_odds_threshold,
        "note": (
            "exclude_top_n_hitsは、的中1件ごとの純利益(payout-stake)が大きい順に"
            "上位1/5/10件を『無かったこと』にした場合の残り損益。"
            "exclude_by_odds_thresholdは、指定オッズ以上の的中があった買い目を除いた"
            "場合の残り損益(投資額はそのまま、対象の払戻だけを除く)。"
            "still_profitable=falseの閾値があれば、その分だけ現在の黒字が"
            "少数の高オッズ的中に依存していることを意味する。読み取り専用の感度分析であり、"
            "この結果を根拠に自動で除外する変更は行っていない。"
        ),
    }


@router.get("/gate-expectancy-detail")
def diagnostics_gate_expectancy_detail(
    since: Optional[str] = Query("calibration_switch"),
    min_samples: int = 50,
    db: Session = Depends(get_db),
):
    """
    券種別実績ゲート(get_bet_type_expectancy_map)が「黒字/赤字」と判定した
    根拠を、実購入分と見送り分に分けて可視化する(読み取り専用・購入判定は
    変更しない)。

    2026-09-07: 券種×オッズ帯ゲートに続き、券種ゲートにも見送り(SkippedBet)の
    実結果を合算する修正を入れたところ、実購入0件で継続赤字だったワイドの
    ゲートが外れて投票プランに登場する事象が発生した。見送り8,000件超を仮想
    100円ずつ賭けたと仮定した平均が黒字化しただけなのか、実際に妥当な検出
    なのかを切り分けるために追加。
    """
    since_dt = purchases_router._parse_since_param(since) if since != "all" else None
    since_dt_eff = since_dt or purchases_router.CALIBRATION_SWITCH_AT

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.purchased_at >= since_dt_eff)
        .all()
    )
    skipped = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.isnot(None))
        .filter(models.SkippedBet.created_at >= since_dt_eff)
        .all()
    )

    by_type: Dict[str, Dict[str, Any]] = {}

    def bucket(bt: str) -> Dict[str, Any]:
        return by_type.setdefault(bt, {
            "purchased": {"n": 0, "stake": 0.0, "payout": 0.0, "wins": 0},
            "skipped": {"n": 0, "stake": 0.0, "payout": 0.0, "wins": 0, "win_payouts": []},
        })

    for p in purchases:
        b = bucket(p.bet_type)["purchased"]
        b["n"] += 1
        b["stake"] += float(p.stake_amount or 0)
        b["payout"] += float(p.payout_amount or 0)
        if p.result == "win":
            b["wins"] += 1

    for s in skipped:
        b = bucket(s.bet_type)["skipped"]
        b["n"] += 1
        b["stake"] += 100.0
        payout = float(s.actual_payout or 0.0)
        b["payout"] += payout
        if s.actual_result == "win":
            b["wins"] += 1
            b["win_payouts"].append(payout)

    out = []
    for bt, v in by_type.items():
        pu, sk = v["purchased"], v["skipped"]
        n_total = pu["n"] + sk["n"]
        stake_total = pu["stake"] + sk["stake"]
        payout_total = pu["payout"] + sk["payout"]
        expectancy_pct = (
            round((payout_total - stake_total) / stake_total * 100, 2) if stake_total > 0 else None
        )
        sk_win_payouts_desc = sorted(sk["win_payouts"], reverse=True)
        sk_expectancy_pct = (
            round((sk["payout"] - sk["stake"]) / sk["stake"] * 100, 2) if sk["stake"] > 0 else None
        )
        pu_expectancy_pct = (
            round((pu["payout"] - pu["stake"]) / pu["stake"] * 100, 2) if pu["stake"] > 0 else None
        )
        out.append({
            "bet_type": bt,
            "n_total": n_total,
            "gate_would_open": (n_total >= min_samples) and (expectancy_pct is not None and expectancy_pct >= 0.0),
            "expectancy_pct_combined": expectancy_pct,
            "purchased": {
                "n": pu["n"],
                "win_count": pu["wins"],
                "stake_total": round(pu["stake"], 0),
                "payout_total": round(pu["payout"], 0),
                "expectancy_pct": pu_expectancy_pct,
            },
            "skipped": {
                "n": sk["n"],
                "win_count": sk["wins"],
                "stake_total(仮想100円換算)": round(sk["stake"], 0),
                "payout_total": round(sk["payout"], 0),
                "expectancy_pct": sk_expectancy_pct,
                # 見送り側の黒字が、少数の的中payoutに偏っていないかを見る指標。
                "top1_win_payout": round(sk_win_payouts_desc[0], 0) if sk_win_payouts_desc else None,
                "top1_win_payout_share_of_skip_payout_pct": (
                    round(100.0 * sk_win_payouts_desc[0] / sk["payout"], 2)
                    if sk_win_payouts_desc and sk["payout"] > 0 else None
                ),
                "top5_win_payout_share_of_skip_payout_pct": (
                    round(100.0 * sum(sk_win_payouts_desc[:5]) / sk["payout"], 2)
                    if sk_win_payouts_desc and sk["payout"] > 0 else None
                ),
            },
        })

    out.sort(key=lambda r: (r["expectancy_pct_combined"] is None, -(r["expectancy_pct_combined"] or -1e9)))

    return {
        "since": since,
        "since_resolved": since_dt_eff.isoformat(),
        "min_samples": min_samples,
        "by_bet_type": out,
        "note": (
            "expectancy_pct_combined が get_bet_type_expectancy_map の判定と同じ値。"
            "purchased.expectancy_pct は実購入分だけの実績(サンプルが少ない券種は"
            "参考程度)。skipped.expectancy_pct は見送りを仮想100円で賭けたと仮定した"
            "場合の実績。top1/top5_win_payout_share_pct が高いほど、少数の大穴的中が"
            "その券種の『黒字』を作っている度合いが強く、結果の頑健性が低いことを示す。"
        ),
    }


@router.get("/pick-to-bet-funnel")
def diagnostics_pick_to_bet_funnel(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    ChatGPT分析9項目の項目2: 本命予想→買い目確率→EVの変換過程の可視化
    (読み取り専用)。

    「本命車番の予測は比較的整っているのに、買い目として展開した段階で
    収益予測が過大になる」可能性を検証する。本命車番自体の予測精度は
    `/purchases/car-pick-accuracy` で確認済みのため、ここでは実購入
    (3連単・2車単、着順を指定する券種)を「本命車番が1着位置の買い目」と
    「本命以外が1着位置の買い目」に分け、それぞれの予測確率と実際の的中率の
    比(キャリブレーション)を比較する。既存の予想ロジック(Harville型の
    組み合わせ確率計算)は一切変更しない。
    """
    since_dt = purchases_router._parse_since_param(since) if since != "all" else None
    q = (
        db.query(models.Purchase)
        .filter(models.Purchase.result.in_(("win", "lose")))
        .filter(models.Purchase.bet_type == "3連単")
    )
    if since_dt is not None:
        q = q.filter(models.Purchase.purchased_at >= since_dt)
    all_rows = q.all()
    # win_prob_at_purchaseが無い古い行はwin_prob_rawで補完する(既存コードの
    # purchases.py 89行目付近と同じフォールバック)。これをしないと対象が
    # 数百件規模まで激減し、本命絡み/非絡みの差を見るには足りなくなる
    # (2026-09-07: since=allで実測149件しかなく発覚し修正)。
    purchases = []
    for p in all_rows:
        prob = getattr(p, "win_prob_raw", None)
        if prob is None:
            prob = p.win_prob_at_purchase
        if prob is not None:
            purchases.append((p, float(prob)))

    if not purchases:
        return {"message": "対象データがありません(3連単・2車単の確定済み購入)"}

    race_ids = {p.race_id for p, _ in purchases}
    races = db.query(models.Race).filter(models.Race.id.in_(race_ids)).all()
    honmei_by_race: Dict[int, Optional[int]] = {}
    for r in races:
        entries = [e for e in r.entries if e.blended_win_prob is not None]
        if not entries:
            honmei_by_race[r.id] = None
            continue
        top = max(entries, key=lambda e: e.blended_win_prob)
        honmei_by_race[r.id] = top.car_number

    def _first_car(combination: str) -> Optional[int]:
        try:
            return int(str(combination).split("-")[0])
        except (ValueError, IndexError):
            return None

    def _stats(rows: List[Tuple[models.Purchase, float]]) -> Optional[Dict[str, Any]]:
        n = len(rows)
        if n == 0:
            return None
        wins = sum(1 for p, _ in rows if p.result == "win")
        pred_avg = sum(prob for _, prob in rows) / n
        act = wins / n
        p_value = calc.binomial_lower_tail_p(wins, n, pred_avg)
        stake = sum(float(p.stake_amount or 0) for p, _ in rows)
        payout = sum(float(p.payout_amount or 0) for p, _ in rows)
        return {
            "n": n,
            "win_count": wins,
            "predicted_win_rate_pct": round(pred_avg * 100, 2),
            "actual_win_rate_pct": round(act * 100, 2),
            "ratio_actual_over_predicted": round(act / pred_avg, 3) if pred_avg > 1e-12 else None,
            "significance_p_value_pct": round(p_value * 100, 4),
            "roi_pct": round(payout / stake * 100, 2) if stake > 0 else None,
        }

    by_bet_type = {}
    for bt in ("3連単", "2車単"):
        rows = [(p, prob) for p, prob in purchases if p.bet_type == bt]
        honmei_first, other_first, unknown = [], [], 0
        for p, prob in rows:
            honmei = honmei_by_race.get(p.race_id)
            first = _first_car(p.combination)
            if honmei is None or first is None:
                unknown += 1
                continue
            (honmei_first if first == honmei else other_first).append((p, prob))
        by_bet_type[bt] = {
            "本命車番が1着位置の買い目": _stats(honmei_first),
            "本命以外が1着位置の買い目": _stats(other_first),
            "本命車番不明で除外した件数": unknown,
        }

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "by_bet_type": by_bet_type,
        "note": (
            "本命車番(そのレースでAIのblended_win_probが最も高い車番)が1着位置に"
            "指定された買い目と、それ以外を分けて、予測確率と実際の的中率の比を"
            "比較する。ratio_actual_over_predictedが1から離れているほど、その"
            "区分での確率変換(本命確率→買い目確率)に偏りがある可能性を示す"
            "(1未満=予測が楽観的、1超=予測が悲観的)。両区分でほぼ同じ比率なら"
            "『買い目展開段階で収益予測が過大になっている』とは言えない。"
            "本命車番自体の予測精度は /purchases/car-pick-accuracy を参照。"
            "既存の予想ロジックは変更していない(読み取り専用の診断)。"
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
        "prob_calibration_grid": diagnostics_prob_calibration_grid(since=since, db=db),
        "ev_band_detail": diagnostics_ev_band_detail(since=since, db=db),
        "discarded_hits": diagnostics_discarded_hits(since=since, db=db),
        "mid_vs_high_ev": diagnostics_mid_vs_high_ev(since=since, db=db),
        "stage_gate_off_virtual": diagnostics_stage_gate_off_virtual(since=since, db=db),
        "ev_negative_recheck": diagnostics_ev_negative_recheck(since=since, db=db),
        "race_plan_design": diagnostics_race_plan_design(),
        "race_plan_rank_compare": diagnostics_race_plan_rank_compare(since=since, db=db),
        "winning_capture": diagnostics_winning_capture(since=since, db=db),
        "reuse_note": (
            "過去サンプルの再利用: 既存Race/Entry/Oddsに対して"
            "race-plan再実行→confirm-resultし直せば、Purchase/Skippedを"
            "現行ロジックで作り直せる。その後本診断を再実行すればよい。"
            "ただしステージ実績ゲートに未来情報が混入している場合は"
            "as-of修正後に再実行すること。"
        ),
    }


def _real_purchase_roi_stats(rows: List[models.Purchase]) -> Optional[Dict[str, Any]]:
    """実購入(Purchase)のリストから、投資額・払戻額ベースの実績ROIを計算する。"""
    n = len(rows)
    if n == 0:
        return None
    wins = sum(1 for p in rows if p.result == "win")
    stake = sum(p.stake_amount or 0 for p in rows)
    payout = sum(p.payout_amount or 0 for p in rows)
    return {
        "件数": n,
        "的中数": wins,
        "的中率%": round(wins / n * 100, 2),
        "総投資額": round(stake, 0),
        "総払戻額": round(payout, 0),
        "実績ROI%": round(payout / stake * 100, 2) if stake > 0 else None,
        "損益": round(payout - stake, 0),
    }


@router.get("/high-odds-correction-check")
def diagnostics_high_odds_correction_check(db: Session = Depends(get_db)):
    """
    実績マイナスの原因調査(特にワイド)用の読み取り専用診断。

    1. 券種別「全期間・実購入のみ」の実績ROI(sinceによる絞り込みは行わない。
       直近数日だけでは母数が小さすぎて判断できないため)。
    2. ワイドについてはオッズ帯別に内訳を出す(ワイドの利益が出ない原因が
       特定のオッズ帯に集中しているかを確認するため)。
    3. 高オッズ帯(300-1000/1000-3000/3000倍以上)について、race-planで
       実際に掛かっている2つの独立した補正係数
       (第2段補正 purchase_set_factor と 方針B補正 high_odds_residual)を
       並べて表示し、同じオッズ帯に対して二重に補正がかかっていないかを確認する。

    既存の予想ロジック・投票ロジックは変更しない(読み取り専用)。
    """
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result.in_(("win", "lose")))
        .all()
    )

    by_bet_type_all_time: Dict[str, Any] = {}
    for bt in sorted({p.bet_type for p in purchases if p.bet_type}):
        by_bet_type_all_time[bt] = _real_purchase_roi_stats(
            [p for p in purchases if p.bet_type == bt]
        )

    # ワイドはオッズ帯別に細分化する
    wide_by_odds_band: Dict[str, List[models.Purchase]] = {}
    for p in purchases:
        if p.bet_type != "ワイド":
            continue
        band = purchases_router.odds_band_label(p.odds_at_purchase)
        wide_by_odds_band.setdefault(band, []).append(p)
    wide_by_odds_band_stats = {
        band: _real_purchase_roi_stats(rows)
        for band, rows in sorted(wide_by_odds_band.items())
    }

    # 全券種×オッズ帯(全期間)も参考として出す
    by_bt_odds_all_time: Dict[str, Dict[str, List[models.Purchase]]] = {}
    for p in purchases:
        band = purchases_router.odds_band_label(p.odds_at_purchase)
        by_bt_odds_all_time.setdefault(p.bet_type, {}).setdefault(band, []).append(p)
    by_bt_odds_all_time_stats = {
        bt: {band: _real_purchase_roi_stats(rows) for band, rows in bands.items()}
        for bt, bands in by_bt_odds_all_time.items()
    }

    # 高オッズ帯で2つの補正係数が両方掛かっているか確認する
    purchase_set_factors = purchases_router.get_purchase_set_calibration_factors(db, use_cache=False)
    high_odds_factors = purchases_router.get_high_odds_residual_factors(db, use_cache=False)

    double_correction_check: Dict[str, Any] = {}
    for band in purchases_router.HIGH_ODDS_BANDS:
        stage2a = (purchase_set_factors.get("by_odds_band") or {}).get(band)
        stage2b = (high_odds_factors.get("by_odds_band") or {}).get(band)
        f2a = stage2a.get("factor") if stage2a else None
        f2b = stage2b.get("factor") if stage2b else None
        combined = round(f2a * f2b, 4) if (f2a is not None and f2b is not None) else None
        double_correction_check[band] = {
            "第2段補正(purchase_set_factor)の係数": f2a,
            "第2段補正の対象件数": stage2a.get("n") if stage2a else None,
            "方針B補正(high_odds_residual)の係数": f2b,
            "方針B補正の対象件数": stage2b.get("n") if stage2b else None,
            "実際にrace-planで掛かる合計倍率(2つの積)": combined,
        }

    # 2026-09-08追加(のんの指摘): 「母数不足」仮説を全期間データで直接検証する。
    # 券種ごとにEV帯(100-105%の薄い帯〜150%以上の厚い帯)へ分解し、
    # EVが高い(=モデルが自信を持っている)帯ほど実績ROIも改善するかを見る。
    # 母数不足が原因なら帯ごとの傾向はバラバラになりやすいが、
    # 「的中率が高い券種ほど実績が悪い」という逆相関が本物なら、
    # EVが高い帯でもROIが改善しない(ev_rank_correlation=broken)はず。
    rows_all = [_purchase_row(p) for p in purchases]
    ev_band_by_bet_type: Dict[str, Any] = {}
    for bt in sorted({r["bet_type"] for r in rows_all}):
        subset = [r for r in rows_all if r["bet_type"] == bt]
        bands_out = []
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
            bands_out.append(stat)
        ev_band_by_bet_type[bt] = {
            "bands": bands_out,
            **_ev_rank_correlation(bands_out),
        }

    return {
        "note": (
            "全期間・実購入のみを対象にした読み取り専用診断(sinceによる絞り込みなし)。"
            "券種別実績ROI・ワイドのオッズ帯別内訳・高オッズ帯の二重補正チェック・"
            "券種別EV帯別ROI(母数不足仮説の検証)をまとめて返す。"
            "既存の予想ロジック・投票ロジックは変更していない。"
        ),
        "券種別_全期間実績ROI": by_bet_type_all_time,
        "ワイド_オッズ帯別_全期間実績ROI": wide_by_odds_band_stats,
        "券種×オッズ帯_全期間実績ROI": by_bt_odds_all_time_stats,
        "高オッズ帯_二重補正チェック": double_correction_check,
        "券種別_EV帯別_全期間実績ROI": ev_band_by_bet_type,
        "二重補正チェックの見方": (
            "『実際にrace-planで掛かる合計倍率』が1から離れているほど、"
            "2つの補正が重なって強く(または弱く)確率を歪めている可能性がある。"
            "たとえば0.5倍×0.5倍=0.25倍のように積が極端に小さい場合、"
            "意図せず過剰に確率を縮小している可能性が高い。"
        ),
    }


def _line_position_map(race) -> Optional[Dict[int, int]]:
    """race.lines_data から {車番: ライン内の並び順(0=先頭,1=番手,2=3番手...)} を作る。"""
    if not race or not race.lines_data:
        return None
    pos_map: Dict[int, int] = {}
    for line in race.lines_data:
        for pos, car in enumerate(line):
            try:
                pos_map[int(car)] = pos
            except (TypeError, ValueError):
                pass
    return pos_map or None


@router.get("/line-boost-sweep")
def diagnostics_line_boost_sweep(db: Session = Depends(get_db)):
    """
    3連単の2・3着展開予測精度向上の第一歩(のんの承認・2026-09-08)。

    現在の確率モデルは、AIが予測する「1着になる確率」しか持っておらず、
    2着・3着はHarville式(1着を除いた残りの中から確率比で機械的に按分)で
    導出しているだけ。競輪特有の「同ラインの選手が連続して上位に来やすい」
    力学を反映するために line_boost という係数を掛けていたが、2026-09-11確定で本番は1.0(補正なし)。
    この値は一度も実績データで検証されたことがない(PROGRESS.md記載の
    既知の未検証項目)。

    このエンドポイントは、line_boostの候補値ごとに「実際に的中した3連単の
    組み合わせに、モデルがどれだけ高い確率を割り当てていたか」を
    対数尤度(log-likelihood)で比較し、どの値が最も実績に合うかを検証する。
    あわせて、1着→2着が同ラインだった場合とそうでない場合、さらに
    ライン内の並び順(先頭/番手/3番手)別にも分解する。

    本番の投票ロジック(app/routers/ev.py)は一切呼び出さない、
    読み取り専用の遡及検証。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    CANDIDATES = [
        0.5, 0.7, 0.8, 0.9,
        1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0, 2.5,
        3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0
    ]

    def _new_stat():
        return {"n": 0, "log_likelihood_sum": 0.0, "prob_sum": 0.0, "zero_count": 0}

    stats_overall = {c: _new_stat() for c in CANDIDATES}
    stats_same_line_12 = {c: _new_stat() for c in CANDIDATES}
    stats_diff_line_12 = {c: _new_stat() for c in CANDIDATES}
    # 1着が先頭(pos=0)のとき、2着が番手(pos=1・同ライン)だったか否か
    stats_head_then_bante = {c: _new_stat() for c in CANDIDATES}
    stats_head_then_other = {c: _new_stat() for c in CANDIDATES}

    evaluated = 0
    skipped_no_win_probs = 0
    skipped_no_result = 0

    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            skipped_no_win_probs += 1
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped_no_result += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped_no_result += 1
            continue
        actual_order = tuple(canonical[0][:3])
        if len(actual_order) < 3 or not all(c in win_probs for c in actual_order):
            skipped_no_result += 1
            continue

        line_map, _ = calc.line_map_from_race(race)
        pos_map = _line_position_map(race)

        same_line_12 = bool(
            line_map
            and line_map.get(actual_order[0]) is not None
            and line_map.get(actual_order[0]) == line_map.get(actual_order[1])
        )
        head_then_bante = bool(
            pos_map
            and pos_map.get(actual_order[0]) == 0
            and same_line_12
            and pos_map.get(actual_order[1]) == 1
        )
        head_win = bool(pos_map and pos_map.get(actual_order[0]) == 0)

        evaluated += 1
        for c in CANDIDATES:
            p = calc.harville_prob(win_probs, actual_order, line_map, c)
            st = stats_overall[c]
            st["n"] += 1
            st["prob_sum"] += p
            if p > 1e-12:
                st["log_likelihood_sum"] += math.log(p)
            else:
                st["zero_count"] += 1

            target = stats_same_line_12[c] if same_line_12 else stats_diff_line_12[c]
            target["n"] += 1
            target["prob_sum"] += p
            if p > 1e-12:
                target["log_likelihood_sum"] += math.log(p)
            else:
                target["zero_count"] += 1

            if head_win:
                target2 = stats_head_then_bante[c] if head_then_bante else stats_head_then_other[c]
                target2["n"] += 1
                target2["prob_sum"] += p
                if p > 1e-12:
                    target2["log_likelihood_sum"] += math.log(p)
                else:
                    target2["zero_count"] += 1

    def _summarize(stats: dict) -> List[dict]:
        out = []
        for c in CANDIDATES:
            st = stats[c]
            n = st["n"]
            out.append({
                "line_boost候補": c,
                "件数": n,
                "平均対数尤度": round(st["log_likelihood_sum"] / n, 5) if n else None,
                "平均予測確率%": round(st["prob_sum"] / n * 100, 4) if n and "prob_sum" in st else None,
                "確率ほぼ0件数": st["zero_count"],
            })
        return out

    overall_summary = _summarize(stats_overall)
    best = max(
        (r for r in overall_summary if r["平均対数尤度"] is not None),
        key=lambda r: r["平均対数尤度"],
        default=None,
    )

    return {
        "note": (
            "平均対数尤度は、実際に的中した3連単の組み合わせにモデルが割り当てていた"
            "確率の対数の平均。0に近いほど良く、マイナスに大きいほど「実際に起きた"
            "ことをモデルが起こりにくいと見誤っていた」ことを意味する。"
            "候補値を比較して、どのline_boostが最も実績に合うかを確認する。"
            "現在の本番値は1.2(検証されないまま使われていた)。"
            "本番ロジック(ev.py)は一切呼び出していない読み取り専用診断。"
        ),
        "評価対象レース数": evaluated,
        "除外(勝率データ無し)": skipped_no_win_probs,
        "除外(結果パース不可)": skipped_no_result,
        "line_boost候補別_全体": overall_summary,
        "最も当てはまりの良いline_boost候補": best["line_boost候補"] if best else None,
        "line_boost候補別_1着2着が同ラインの場合": _summarize(stats_same_line_12),
        "line_boost候補別_1着2着が別ラインの場合": _summarize(stats_diff_line_12),
        "line_boost候補別_1着が先頭→2着が番手だった場合": _summarize(stats_head_then_bante),
        "line_boost候補別_1着が先頭→2着が番手以外だった場合": _summarize(stats_head_then_other),
        "読み方": (
            "『1着2着が同ライン』の平均対数尤度が『別ライン』より大きく改善する"
            "line_boost値があれば、それが実績に合った値。"
            "『先頭→番手』と『先頭→番手以外』で改善幅が大きく違う場合、"
            "同ラインの中でも並び順(先頭/番手/3番手)を区別して補正すべきという根拠になる。"
        ),
    }


@router.get("/trifecta-order-structure")
def diagnostics_trifecta_order_structure(
    db: Session = Depends(get_db),
    line_boost: float = Query(1.0, description="検証に使うline_boost値(既定は本番値1.0=補正なし)"),
):
    """
    課題J(3連単の条件付き着順構造)に対応する読み取り専用診断。

    現行モデルは「1着確率」しか直接予測しておらず、2着・3着はHarville式で
    機械的に導出しているだけ。この診断では過去の全確定レースについて、

    1. 1着予測(勝率最大の車)が実際に当たったか
    2. 実際の1着車を固定した条件で、2着候補の予測順位・確率が実際とどれだけ合うか
       (=1着→2着の条件付き精度)
    3. 実際の1着・2着車を固定した条件で、3着候補の予測がどれだけ合うか
       (=1着2着→3着の条件付き精度)
    4. 全3連単組み合わせ(車番の並び全通り)を確率順に並べたとき、
       実際の的中組み合わせが上位何位に入っていたか(Top-1/3/5/10/20/30包含率)
    5. 「1着予測が当たったレース」と「外れたレース」で、Top-k包含率が
       どれだけ違うか(=1着の誤りと2・3着展開の誤りを分離して評価)

    をまとめて計算する。本番ロジック(ev.py)は一切呼び出さない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    TOPK = [1, 3, 5, 10, 20, 30]

    n_races = 0
    n_1st_correct = 0

    # 1着→2着の条件付き精度
    cond2_n = 0
    cond2_top_correct = 0
    cond2_prob_sum = 0.0  # 実際の2着車に割り当てられた条件付き確率の合計(平均を出す)

    # 1着2着→3着の条件付き精度
    cond3_n = 0
    cond3_top_correct = 0
    cond3_prob_sum = 0.0

    # Top-k包含率(全体、1着的中時、1着不的中時で分ける)
    def _new_topk():
        return {k: 0 for k in TOPK}

    topk_hits_all = _new_topk()
    topk_hits_1st_correct = _new_topk()
    topk_hits_1st_wrong = _new_topk()
    n_1st_correct_evaluated = 0
    n_1st_wrong_evaluated = 0

    skipped_no_win_probs = 0
    skipped_no_result = 0
    skipped_too_many_entries = 0

    MAX_ENTRIES_FOR_FULL_RANK = 9  # 9車立てまでなら全順列(504通り)を計算

    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs or len(win_probs) < 3:
            skipped_no_win_probs += 1
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped_no_result += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped_no_result += 1
            continue
        actual_order = tuple(canonical[0][:3])
        if len(actual_order) < 3 or not all(c in win_probs for c in actual_order):
            skipped_no_result += 1
            continue

        line_map, _ = calc.line_map_from_race(race)
        cars = list(win_probs.keys())
        n_races += 1

        # 1. 1着予測
        predicted_1st = max(win_probs, key=win_probs.get)
        first_correct = predicted_1st == actual_order[0]
        if first_correct:
            n_1st_correct += 1

        # 2. 1着→2着の条件付き精度(実際の1着車を固定)
        remaining_after_1st = {c: v for c, v in win_probs.items() if c != actual_order[0]}
        if remaining_after_1st:
            if line_map and line_boost != 1.0:
                boosted = {
                    c: (v * line_boost if line_map.get(c) == line_map.get(actual_order[0]) else v)
                    for c, v in remaining_after_1st.items()
                }
            else:
                boosted = remaining_after_1st
            denom2 = sum(boosted.values())
            if denom2 > 1e-9:
                cond2_n += 1
                predicted_2nd = max(boosted, key=boosted.get)
                if predicted_2nd == actual_order[1]:
                    cond2_top_correct += 1
                cond2_prob_sum += boosted.get(actual_order[1], 0.0) / denom2

        # 3. 1着2着→3着の条件付き精度(実際の1着・2着車を固定)
        remaining_after_2nd = {c: v for c, v in win_probs.items() if c not in (actual_order[0], actual_order[1])}
        if remaining_after_2nd:
            if line_map and line_boost != 1.0:
                boosted3 = {
                    c: (v * line_boost if line_map.get(c) == line_map.get(actual_order[1]) else v)
                    for c, v in remaining_after_2nd.items()
                }
            else:
                boosted3 = remaining_after_2nd
            denom3 = sum(boosted3.values())
            if denom3 > 1e-9:
                cond3_n += 1
                predicted_3rd = max(boosted3, key=boosted3.get)
                if predicted_3rd == actual_order[2]:
                    cond3_top_correct += 1
                cond3_prob_sum += boosted3.get(actual_order[2], 0.0) / denom3

        # 4. Top-k包含率(全組み合わせを確率順に並べる)
        if len(cars) > MAX_ENTRIES_FOR_FULL_RANK:
            skipped_too_many_entries += 1
            continue
        scored = []
        for perm in itertools.permutations(cars, 3):
            p = calc.harville_prob(win_probs, perm, line_map, line_boost)
            scored.append((p, perm))
        scored.sort(key=lambda x: x[0], reverse=True)
        rank = None
        for idx, (_, perm) in enumerate(scored):
            if perm == actual_order:
                rank = idx + 1
                break
        if rank is not None:
            for k in TOPK:
                if rank <= k:
                    topk_hits_all[k] += 1
            if first_correct:
                n_1st_correct_evaluated += 1
                for k in TOPK:
                    if rank <= k:
                        topk_hits_1st_correct[k] += 1
            else:
                n_1st_wrong_evaluated += 1
                for k in TOPK:
                    if rank <= k:
                        topk_hits_1st_wrong[k] += 1

    def _topk_table(hits: dict, n: int) -> List[dict]:
        return [
            {
                "Top-k": k,
                "包含件数": hits[k],
                "包含率%": round(hits[k] / n * 100, 2) if n else None,
            }
            for k in TOPK
        ]

    return {
        "note": (
            "全期間・確定済みレースを対象にした読み取り専用診断(課題J対応)。"
            "本番ロジック(ev.py)は一切呼び出していない。line_boostは"
            f"クエリパラメータで変更可能(既定{line_boost}=本番値)。"
        ),
        "評価対象レース数": n_races,
        "除外(勝率データ無し)": skipped_no_win_probs,
        "除外(結果パース不可)": skipped_no_result,
        "除外(出走9車超のためTop-k計算スキップ)": skipped_too_many_entries,
        "1着予測精度": {
            "件数": n_races,
            "的中数": n_1st_correct,
            "的中率%": round(n_1st_correct / n_races * 100, 2) if n_races else None,
        },
        "1着固定時の2着的中精度(条件付き)": {
            "件数": cond2_n,
            "最有力候補が2着的中した数": cond2_top_correct,
            "最有力候補の的中率%": round(cond2_top_correct / cond2_n * 100, 2) if cond2_n else None,
            "実際の2着車に割り当てた平均条件付き確率%": round(cond2_prob_sum / cond2_n * 100, 2) if cond2_n else None,
        },
        "1着2着固定時の3着的中精度(条件付き)": {
            "件数": cond3_n,
            "最有力候補が3着的中した数": cond3_top_correct,
            "最有力候補の的中率%": round(cond3_top_correct / cond3_n * 100, 2) if cond3_n else None,
            "実際の3着車に割り当てた平均条件付き確率%": round(cond3_prob_sum / cond3_n * 100, 2) if cond3_n else None,
        },
        "Top-k的中組み合わせ包含率_全体": _topk_table(topk_hits_all, n_1st_correct_evaluated + n_1st_wrong_evaluated),
        "Top-k的中組み合わせ包含率_1着予測が的中したレース": _topk_table(topk_hits_1st_correct, n_1st_correct_evaluated),
        "Top-k的中組み合わせ包含率_1着予測が外れたレース": _topk_table(topk_hits_1st_wrong, n_1st_wrong_evaluated),
        "読み方": (
            "『1着固定時の2着的中精度』が低い場合、2着・3着の展開予測(Harville式+"
            "line_boost)そのものに改善余地がある。"
            "『1着予測が的中したレースのTop-k』と『外れたレースのTop-k』の差が大きい場合、"
            "3連単全体の誤差の大部分は1着予測の誤りに起因しており、2・3着の展開予測を"
            "いくら改善しても効果が限定的である可能性が高い。逆に差が小さい場合は、"
            "1着が当たっても外れても2・3着展開の誤差が支配的であり、"
            "展開予測(line_boost・脚質・競走得点等)の改善効果が期待できる。"
        ),
    }


def _harville_prob_v2(
    win_probs: Dict[int, float],
    order: Tuple[int, ...],
    line_map: Optional[Dict[int, int]],
    pos_map: Optional[Dict[int, int]],
    head_to_bante_boost: float,
    other_same_line_boost: float,
) -> float:
    """
    harville_probの2パラメータ版(読み取り専用診断でのみ使用。本番コードには反映しない)。
    「先頭選手が確定した直後に、同ラインの番手選手が続く」場合だけ
    head_to_bante_boostを掛け、それ以外の同ライン継続はother_same_line_boostを掛ける。
    """
    remaining = dict(win_probs)
    prob = 1.0
    prev_car = None
    for car in order:
        p = remaining.get(car, 0.0)
        if line_map and prev_car is not None:
            same_line = line_map.get(car) is not None and line_map.get(car) == line_map.get(prev_car)
        else:
            same_line = False
        if same_line:
            is_head_to_bante = (
                pos_map is not None
                and pos_map.get(prev_car) == 0
                and pos_map.get(car) == 1
            )
            boost = head_to_bante_boost if is_head_to_bante else other_same_line_boost
        else:
            boost = 1.0
        if boost != 1.0 and line_map:
            boosted = {}
            for c, v in remaining.items():
                if line_map.get(c) == line_map.get(prev_car):
                    if pos_map is not None and pos_map.get(prev_car) == 0 and pos_map.get(c) == 1:
                        boosted[c] = v * head_to_bante_boost
                    else:
                        boosted[c] = v * other_same_line_boost
                else:
                    boosted[c] = v
            denom = sum(boosted.values())
            cond_p = (boosted.get(car, 0.0) / denom) if denom > 1e-9 else 0.0
        else:
            denom = sum(remaining.values())
            cond_p = (p / denom) if denom > 1e-9 else 0.0
        prob *= cond_p
        remaining.pop(car, None)
        prev_car = car
    return prob


@router.get("/line-boost-sweep-v2")
def diagnostics_line_boost_sweep_v2(db: Session = Depends(get_db)):
    """
    先頭→番手boostの頭打ちを検証する読み取り専用診断。

    単純な固定上限探索ではなく、対数スケールで広範囲を探索し、
    各点の対数尤度・前点との差・改善率を返す。

    重要:
    - 本番ロジック(ev.py)は呼び出さない
    - boostを本番値へ変更しない
    - 自動最適化はしない
    - 「探索上限に到達した」のか「改善が飽和した」のかを分離する
    """

    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    # 低い領域は細かく、高い領域は桁単位で確認する。
    # 1,000万倍まで探索するが、頭打ち判定が成立した時点で
    # それ以上を「必要な探索」とは扱わない。
    HEAD_CANDIDATES = [
        1.0, 1.2, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0,
        15.0, 20.0, 30.0, 50.0, 80.0, 120.0,
        200.0, 300.0, 500.0, 800.0, 1200.0,
        2000.0, 3000.0, 5000.0, 8000.0, 12000.0,
        20000.0, 30000.0, 50000.0, 80000.0,
        120000.0, 200000.0, 300000.0, 500000.0,
        800000.0, 1200000.0, 2000000.0, 3000000.0,
        5000000.0, 8000000.0, 10000000.0,
    ]

    OTHER_CANDIDATES = [0.8, 1.0, 1.2]

    # レスポンス用の共通評価件数。
    # boost系列ごとに再計算するが、評価対象レース自体は共通。
    evaluated = 0
    skipped_no_win_probs = 0
    skipped_no_result = 0

    # まず実際に評価可能なレースを確定する。
    valid_races = []

    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            skipped_no_win_probs += 1
            continue

        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped_no_result += 1
            continue

        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped_no_result += 1
            continue

        actual_order = tuple(canonical[0][:3])

        if len(actual_order) < 3 or not all(c in win_probs for c in actual_order):
            skipped_no_result += 1
            continue

        line_map, _ = calc.line_map_from_race(race)
        pos_map = _line_position_map(race)

        valid_races.append((win_probs, actual_order, line_map, pos_map))

    evaluated = len(valid_races)

    # 頭打ち判定。
    # 平均対数尤度の改善量が極めて小さい状態が3点連続したら、
    # その系列について以降のboost探索を打ち切る。
    SATURATION_DELTA = 0.00001
    SATURATION_RELATIVE_PCT = 0.0001
    CONSECUTIVE_SATURATION = 3

    results = []

    for other_boost in OTHER_CANDIDATES:
        series = []
        previous_ll = None
        saturation_streak = 0
        saturation_at = None

        for head_boost in HEAD_CANDIDATES:
            log_likelihood_sum = 0.0
            prob_sum = 0.0
            zero_count = 0
            n = 0

            for win_probs, actual_order, line_map, pos_map in valid_races:
                prob = _harville_prob_v2(
                    win_probs,
                    actual_order,
                    line_map,
                    pos_map,
                    head_boost,
                    other_boost,
                )

                n += 1
                prob_sum += prob

                if prob > 1e-300:
                    log_likelihood_sum += math.log(prob)
                else:
                    zero_count += 1
                    log_likelihood_sum += math.log(1e-300)

            ll = log_likelihood_sum / n if n else None
            avg_prob = prob_sum / n if n else None

            row = {
                "先頭→番手boost": head_boost,
                "それ以外の同ラインboost": other_boost,
                "件数": n,
                "平均対数尤度_raw": ll,
                "平均対数尤度": round(ll, 8) if ll is not None else None,
                "平均予測確率": avg_prob,
                "平均予測確率%": (
                    round(avg_prob * 100.0, 8)
                    if avg_prob is not None else None
                ),
                "確率ほぼ0件数": zero_count,
            }

            if previous_ll is None or ll is None:
                row["前点との差"] = None
                row["改善率_pct"] = None
            else:
                delta = ll - previous_ll
                row["前点との差"] = round(delta, 10)

                if abs(previous_ll) > 1e-15:
                    row["改善率_pct"] = round(
                        delta / abs(previous_ll) * 100.0,
                        8,
                    )
                else:
                    row["改善率_pct"] = None

                is_saturated = (
                    0.0 <= delta < SATURATION_DELTA
                    or (
                        row["改善率_pct"] is not None
                        and 0.0 <= row["改善率_pct"] < SATURATION_RELATIVE_PCT
                    )
                )

                if is_saturated:
                    saturation_streak += 1
                else:
                    saturation_streak = 0

                if (
                    saturation_streak >= CONSECUTIVE_SATURATION
                    and saturation_at is None
                ):
                    saturation_at = head_boost

                    # このboostまでは記録し、それ以降は探索しない。
                    series.append(row)
                    previous_ll = ll
                    break

            series.append(row)
            previous_ll = ll

        valid = [
            r for r in series
            if r["平均対数尤度_raw"] is not None
        ]

        best = (
            max(
                valid,
                key=lambda r: r["平均対数尤度_raw"],
            )
            if valid else None
        )

        results.append({
            "それ以外の同ラインboost": other_boost,
            "系列": [
                {
                    k: v
                    for k, v in row.items()
                    if k != "平均対数尤度_raw"
                }
                for row in series
            ],
            "最良値": best,
            "頭打ち候補": saturation_at,
            "頭打ち判定閾値": SATURATION_DELTA,
            "頭打ち相対改善率_pct": SATURATION_RELATIVE_PCT,
            "連続判定回数": CONSECUTIVE_SATURATION,
            "探索上限到達": bool(
                valid
                and valid[-1]["先頭→番手boost"] == HEAD_CANDIDATES[-1]
            ),
        })

    # other=1.0を主系列として取得。
    main = next(
        (
            r for r in results
            if r["それ以外の同ラインboost"] == 1.0
        ),
        None,
    )

    return {
        "note": (
            "先頭→番手boostの飽和点を検証する読み取り専用診断。"
            "対数スケールで最大10000000倍まで探索し、各点の"
            "平均対数尤度・前点との差・改善率を記録する。"
            "改善が極めて小さい状態が3点連続した場合を頭打ち候補とする。"
            "頭打ち候補は必要条件ではなく、探索を打ち切るための"
            "診断上の目安である。"
        ),
        "評価対象レース数": evaluated,
        "除外(勝率データ無し)": skipped_no_win_probs,
        "除外(結果パース不可)": skipped_no_result,
        "探索上限": HEAD_CANDIDATES[-1],
        "飽和判定": {
            "1レース平均対数尤度の改善量": SATURATION_DELTA,
            "改善率_pct": SATURATION_RELATIVE_PCT,
            "連続回数": CONSECUTIVE_SATURATION,
        },
        "結果": results,
        "主系列_other_boost_1.0": main,
    }


@router.get("/normalization-check")
def diagnostics_normalization_check(db: Session = Depends(get_db), limit: int = Query(30, ge=1, le=200)):
    """
    2026-09-08実装の先頭→番手boost(20倍)・正規化係数(total_ordered_mass)が
    エラー無く動作しているかを確認する読み取り専用診断。
    Termux側にはDATABASE_URLが無く生DBスクリプトが動かせないため、
    サーバー側(DATABASE_URLを持つ)で同じチェックをAPI経由で行えるようにする。
    本番の投票ロジックは一切変更しない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .order_by(models.Race.id.desc())
        .limit(limit)
        .all()
    )

    checked = 0
    errors = []
    mass_samples = []
    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs or len(win_probs) < 3:
            continue
        try:
            line_map, line_boost = calc.line_map_from_race(race)
            pos_map = calc.line_position_map(race)
            cars = sorted(win_probs.keys())
            mass3 = calc.total_ordered_mass(
                win_probs, cars, 3, line_map, line_boost, pos_map, calc.HEAD_TO_BANTE_BOOST
            )
            mass2 = calc.total_ordered_mass(
                win_probs, cars, 2, line_map, line_boost, pos_map, calc.HEAD_TO_BANTE_BOOST
            )
            checked += 1
            mass_samples.append({
                "race_id": race.id,
                "正規化前mass_arity3": round(mass3, 4),
                "正規化前mass_arity2": round(mass2, 4),
                "ライン情報あり": line_map is not None,
            })
        except Exception as e:
            errors.append({"race_id": race.id, "error": str(e)})

    return {
        "note": (
            "先頭→番手boost(20倍)・正規化係数(total_ordered_mass)の動作確認。"
            "本番ロジックは一切変更していない読み取り専用診断。"
            "『正規化前mass』はboostによる過大カウントの生値。"
            "プラン生成時はこの値で割って正規化されるため、1を超えていて問題ない"
            "(ライン情報が無いレースは1.0付近になるのが正常)。"
        ),
        "確認したレース数": checked,
        "エラー件数": len(errors),
        "エラー詳細": errors,
        "サンプル(直近20件まで表示)": mass_samples[:20],
    }


@router.get("/player-stats-potential")
def diagnostics_player_stats_potential(db: Session = Depends(get_db)):
    """
    課題J/K対応:選手個人データ(app_2nd_rate・app_3rd_rate等)の充足率と、
    現在は全く使われていないこれらの値が、2着・3着予測において
    現行モデル(Harville+先頭→番手boost)より予測力を持つかを検証する。

    Entryテーブルには既にapp_2nd_rate(選手個人の2着率)・app_3rd_rate(3着率)・
    finish_2nd/3rd(2着/3着回数)・kimarite_*(決まり手別勝利数)等が保存されているが、
    現行の確率モデルはapp_win_rate/ai_win_prob由来の1着確率(blended_win_prob)しか
    使っておらず、2着・3着はHarville式で機械的に導出しているだけで、
    これらの個人データを一切参照していない(2026-09-08判明)。

    本番ロジック(ev.py)は一切呼び出さない、読み取り専用の遡及検証。
    """
    total_entries = db.query(models.Entry).count()

    def _availability(column) -> dict:
        n = db.query(models.Entry).filter(column.isnot(None)).count()
        return {"件数": n, "充足率%": round(n / total_entries * 100, 1) if total_entries else None}

    availability = {
        "app_win_rate(1着率)": _availability(models.Entry.app_win_rate),
        "app_2nd_rate(2着率)": _availability(models.Entry.app_2nd_rate),
        "app_3rd_rate(3着率)": _availability(models.Entry.app_3rd_rate),
        "finish_1st(1着回数)": _availability(models.Entry.finish_1st),
        "finish_2nd(2着回数)": _availability(models.Entry.finish_2nd),
        "finish_3rd(3着回数)": _availability(models.Entry.finish_3rd),
        "race_score(競走得点)": _availability(models.Entry.race_score),
        "leg_style(脚質)": _availability(models.Entry.leg_style),
        "kimarite_nige(逃げ決着回数)": _availability(models.Entry.kimarite_nige),
        "kimarite_makuri(捲り決着回数)": _availability(models.Entry.kimarite_makuri),
        "kimarite_sashi(差し決着回数)": _availability(models.Entry.kimarite_sashi),
        "kimarite_mark(マーク決着回数)": _availability(models.Entry.kimarite_mark),
    }

    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    n_2nd_eval = 0
    current_model_2nd_correct_all = 0
    n_2nd_with_rate = 0
    current_model_2nd_correct_subset = 0
    app_2nd_rate_correct = 0
    combined_2nd_correct = 0

    n_3rd_eval = 0
    current_model_3rd_correct_all = 0
    n_3rd_with_rate = 0
    current_model_3rd_correct_subset = 0
    app_3rd_rate_correct = 0

    def _model_pick(remaining: dict, prev_car: int, line_map, pos_map, line_boost: float):
        boosted = {}
        for c, v in remaining.items():
            if line_map and line_map.get(c) == line_map.get(prev_car):
                if pos_map and pos_map.get(prev_car) == 0 and pos_map.get(c) == 1:
                    boosted[c] = v * calc.HEAD_TO_BANTE_BOOST
                else:
                    boosted[c] = v * line_boost
            else:
                boosted[c] = v
        return max(boosted, key=boosted.get)

    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            continue
        actual_order = tuple(canonical[0][:3])
        if len(actual_order) < 3 or not all(c in win_probs for c in actual_order):
            continue

        line_map, line_boost = calc.line_map_from_race(race)
        pos_map = calc.line_position_map(race)
        entry_by_car = {e.car_number: e for e in race.entries}

        # --- 2着予測比較(実際の1着車を固定) ---
        remaining = {c: v for c, v in win_probs.items() if c != actual_order[0]}
        if remaining:
            n_2nd_eval += 1
            model_pick = _model_pick(remaining, actual_order[0], line_map, pos_map, line_boost)
            if model_pick == actual_order[1]:
                current_model_2nd_correct_all += 1

            rate_candidates = {
                c: entry_by_car[c].app_2nd_rate
                for c in remaining
                if c in entry_by_car and entry_by_car[c].app_2nd_rate is not None
            }
            if rate_candidates:
                n_2nd_with_rate += 1
                if model_pick == actual_order[1]:
                    current_model_2nd_correct_subset += 1
                rate_pick = max(rate_candidates, key=rate_candidates.get)
                if rate_pick == actual_order[1]:
                    app_2nd_rate_correct += 1

                remaining_sorted = sorted(remaining, key=lambda c: remaining[c], reverse=True)
                rate_sorted = sorted(rate_candidates, key=lambda c: rate_candidates[c], reverse=True)
                win_rank = {c: i for i, c in enumerate(remaining_sorted)}
                rate_rank = {c: i for i, c in enumerate(rate_sorted)}
                combined_pick = min(
                    rate_candidates,
                    key=lambda c: win_rank.get(c, 999) + rate_rank.get(c, 999),
                )
                if combined_pick == actual_order[1]:
                    combined_2nd_correct += 1

        # --- 3着予測比較(実際の1着・2着車を固定) ---
        remaining2 = {c: v for c, v in win_probs.items() if c not in (actual_order[0], actual_order[1])}
        if remaining2:
            n_3rd_eval += 1
            model_pick3 = _model_pick(remaining2, actual_order[1], line_map, pos_map, line_boost)
            if model_pick3 == actual_order[2]:
                current_model_3rd_correct_all += 1

            rate_candidates3 = {
                c: entry_by_car[c].app_3rd_rate
                for c in remaining2
                if c in entry_by_car and entry_by_car[c].app_3rd_rate is not None
            }
            if rate_candidates3:
                n_3rd_with_rate += 1
                if model_pick3 == actual_order[2]:
                    current_model_3rd_correct_subset += 1
                rate_pick3 = max(rate_candidates3, key=rate_candidates3.get)
                if rate_pick3 == actual_order[2]:
                    app_3rd_rate_correct += 1

    def _pct(n, d):
        return round(n / d * 100, 2) if d else None

    return {
        "note": (
            "選手個人データ(app_2nd_rate等)の充足率と予測力を検証する読み取り専用診断。"
            "本番ロジック(ev.py)は一切呼び出していない。"
        ),
        "データ充足率(全Entry対象)": availability,
        "2着予測の比較": {
            "全体評価件数": n_2nd_eval,
            "現行モデルの的中率%(全体)": _pct(current_model_2nd_correct_all, n_2nd_eval),
            "app_2nd_rateデータあり件数": n_2nd_with_rate,
            "現行モデルの的中率%(データありに限定)": _pct(current_model_2nd_correct_subset, n_2nd_with_rate),
            "app_2nd_rateのみで選んだ場合の的中率%": _pct(app_2nd_rate_correct, n_2nd_with_rate),
            "win_prob順位×app_2nd_rate順位の単純合成の的中率%": _pct(combined_2nd_correct, n_2nd_with_rate),
        },
        "3着予測の比較": {
            "全体評価件数": n_3rd_eval,
            "現行モデルの的中率%(全体)": _pct(current_model_3rd_correct_all, n_3rd_eval),
            "app_3rd_rateデータあり件数": n_3rd_with_rate,
            "現行モデルの的中率%(データありに限定)": _pct(current_model_3rd_correct_subset, n_3rd_with_rate),
            "app_3rd_rateのみで選んだ場合の的中率%": _pct(app_3rd_rate_correct, n_3rd_with_rate),
        },
        "読み方": (
            "『app_2nd_rateのみ』または『単純合成』が『現行モデル(データありに限定)』を"
            "上回っていれば、選手個人の2着率・3着率データを確率計算に組み込む価値がある。"
            "下回っている、または同程度なら、単純に組み込むだけでは改善しない可能性が高く、"
            "別の使い方(交互作用・重み付け等)を検討する必要がある。"
        ),
    }


def _position_pair_category(pos_prev, pos_next, same_line: bool) -> str:
    if not same_line:
        return "別ライン"
    if pos_prev is None or pos_next is None:
        return "同ライン(位置不明)"
    label = {0: "先頭", 1: "番手", 2: "3番手"}
    p = label.get(pos_prev, f"{pos_prev}番目")
    n = label.get(pos_next, f"{pos_next}番目")
    return f"同ライン:{p}→{n}"


@router.get("/line-position-matrix")
def diagnostics_line_position_matrix(db: Session = Depends(get_db)):
    """
    課題L対応:先頭→番手boost(20倍)以外の位置ペア
    (番手→先頭、先頭→3番手、番手→3番手 等)についても、実績に見合った
    補正倍率がありそうかを一括で確認する読み取り専用診断。

    グリッドサーチではなく、位置ペアの区分ごとに
    「実際にその遷移が起きた回数」÷「無補正モデルが割り当てていた確率の合計」
    という経験的な比率(=その区分に必要な補正倍率の目安)を直接計算する。
    比率が1に近い区分は補正不要、大きく離れている区分は補正の余地がある。

    対象は1着→2着・2着→3着の遷移をあわせたもの。
    本番ロジック(ev.py)は一切呼び出さない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    actual_count: Dict[str, int] = defaultdict(int)
    predicted_mass: Dict[str, float] = defaultdict(float)
    opportunity_count: Dict[str, int] = defaultdict(int)

    evaluated_transitions = 0

    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            continue
        actual_order = tuple(canonical[0][:3])
        if len(actual_order) < 3 or not all(c in win_probs for c in actual_order):
            continue

        line_map, _ = calc.line_map_from_race(race)
        pos_map = calc.line_position_map(race)

        # 1着→2着、2着→3着の2つの遷移をそれぞれ評価する
        transitions = [
            (actual_order[0], actual_order[1], {c: v for c, v in win_probs.items() if c != actual_order[0]}),
            (
                actual_order[1],
                actual_order[2],
                {c: v for c, v in win_probs.items() if c not in (actual_order[0], actual_order[1])},
            ),
        ]

        for prev_car, actual_next, remaining in transitions:
            if not remaining:
                continue
            denom = sum(remaining.values())
            if denom <= 1e-9:
                continue
            evaluated_transitions += 1
            for c, v in remaining.items():
                same_line = bool(
                    line_map and line_map.get(c) is not None and line_map.get(c) == line_map.get(prev_car)
                )
                pos_prev = pos_map.get(prev_car) if pos_map else None
                pos_next = pos_map.get(c) if pos_map else None
                cat = _position_pair_category(pos_prev, pos_next, same_line)
                predicted_mass[cat] += v / denom
                opportunity_count[cat] += 1
            same_line_actual = bool(
                line_map and line_map.get(actual_next) is not None and line_map.get(actual_next) == line_map.get(prev_car)
            )
            cat_actual = _position_pair_category(
                pos_map.get(prev_car) if pos_map else None,
                pos_map.get(actual_next) if pos_map else None,
                same_line_actual,
            )
            actual_count[cat_actual] += 1

    results = []
    for cat in sorted(predicted_mass.keys(), key=lambda k: -predicted_mass[k]):
        pm = predicted_mass[cat]
        ac = actual_count.get(cat, 0)
        results.append({
            "区分": cat,
            "候補として現れた延べ回数": opportunity_count.get(cat, 0),
            "無補正モデルの予測確率合計": round(pm, 3),
            "実際にその遷移が起きた回数": ac,
            "経験的補正倍率の目安(実際÷予測)": round(ac / pm, 3) if pm > 1e-6 else None,
        })

    return {
        "note": (
            "位置ペア区分ごとに、実際の遷移回数と無補正モデルの予測確率合計の比率"
            "(=経験的に必要な補正倍率の目安)を算出する読み取り専用診断。"
            "本番ロジック(ev.py)は一切呼び出していない。"
        ),
        "評価対象遷移数(1着→2着+2着→3着)": evaluated_transitions,
        "位置ペア区分別": results,
        "読み方": (
            "『経験的補正倍率の目安』が1から大きく離れている区分(例: 番手→先頭が2倍等)は、"
            "現在の先頭→番手boost(20倍)だけでは捉えられていない追加の力学がある可能性が高い。"
            "候補として現れた延べ回数が少ない区分(数十件未満)は参考程度に留めること"
            "(過学習・偶然の影響を受けやすいため)。"
        ),
    }


@router.get("/race-score-potential")
def diagnostics_race_score_potential(db: Session = Depends(get_db)):
    """
    課題L対応: 競走得点(race_score)の予測力を検証する読み取り専用診断。

    Entry.race_score はほぼ100%充足しているが、現行の確率モデル(Harville +
    head_to_bante_boost)は競走得点を直接使っていない(Geminiがblended_win_probに
    反映している可能性はあるが、明示的な特徴量としては未使用)。

    本診断では以下を測る:
    1. 競走得点順位だけで1着・2着・3着を当てた場合の的中率
    2. 現行モデル(1着確率ランキング)との比較
    3. 実際の1-2-3着間の得点差の分布
    4. 得点帯ごとの1着率

    本番ロジック(ev.py)は一切呼び出さない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    n_eval = 0
    score_1st_correct = 0
    model_1st_correct = 0
    score_2nd_correct = 0
    model_2nd_correct = 0
    score_3rd_correct = 0
    model_3rd_correct = 0

    # 実際の1-2-3着の得点差
    diff_1_2 = []
    diff_1_3 = []
    diff_2_3 = []

    # 得点帯別1着率
    band_stats = {}  # band -> {"n": , "wins": }

    def _score_band(s: float) -> str:
        if s is None:
            return "欠損"
        if s < 90:
            return "90未満"
        if s < 95:
            return "90-95"
        if s < 100:
            return "95-100"
        if s < 105:
            return "100-105"
        if s < 110:
            return "105-110"
        return "110以上"

    skipped_no_score = 0
    skipped_no_result = 0

    for race in races:
        entries = race.entries or []
        if len(entries) < 3:
            continue

        score_map = {}
        for e in entries:
            if e.race_score is not None and e.car_number is not None:
                score_map[int(e.car_number)] = float(e.race_score)

        if len(score_map) < 3:
            skipped_no_score += 1
            continue

        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped_no_result += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped_no_result += 1
            continue
        actual = tuple(canonical[0][:3])
        if len(actual) < 3 or not all(c in score_map for c in actual):
            skipped_no_result += 1
            continue

        # 現行モデルの1着確率
        win_probs = {}
        for e in entries:
            p = e.blended_win_prob
            if p is None and e.app_win_rate is not None:
                p = e.app_win_rate / 100.0
            if p is not None and e.car_number is not None:
                win_probs[int(e.car_number)] = float(p)

        n_eval += 1

        # 得点順位
        by_score = sorted(score_map.keys(), key=lambda c: score_map[c], reverse=True)
        # モデル順位
        by_model = sorted(win_probs.keys(), key=lambda c: win_probs.get(c, 0), reverse=True) if win_probs else []

        if by_score[0] == actual[0]:
            score_1st_correct += 1
        if by_model and by_model[0] == actual[0]:
            model_1st_correct += 1

        # 2着: 1着を除いた残りで最高得点 / 最高確率
        remaining_score = [c for c in by_score if c != actual[0]]
        remaining_model = [c for c in by_model if c != actual[0]]
        if remaining_score and remaining_score[0] == actual[1]:
            score_2nd_correct += 1
        if remaining_model and remaining_model[0] == actual[1]:
            model_2nd_correct += 1

        # 3着
        remaining2_score = [c for c in remaining_score if c != actual[1]]
        remaining2_model = [c for c in remaining_model if c != actual[1]]
        if remaining2_score and remaining2_score[0] == actual[2]:
            score_3rd_correct += 1
        if remaining2_model and remaining2_model[0] == actual[2]:
            model_3rd_correct += 1

        # 得点差
        s1 = score_map[actual[0]]
        s2 = score_map[actual[1]]
        s3 = score_map[actual[2]]
        diff_1_2.append(s1 - s2)
        diff_1_3.append(s1 - s3)
        diff_2_3.append(s2 - s3)

        # 得点帯別1着率（全出走車）
        for car, sc in score_map.items():
            band = _score_band(sc)
            st = band_stats.setdefault(band, {"n": 0, "wins": 0})
            st["n"] += 1
            if car == actual[0]:
                st["wins"] += 1

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    def _median(xs):
        if not xs:
            return None
        s = sorted(xs)
        m = len(s) // 2
        return round(s[m], 2) if len(s) % 2 else round((s[m - 1] + s[m]) / 2, 2)

    band_rows = []
    for band in ["90未満", "90-95", "95-100", "100-105", "105-110", "110以上", "欠損"]:
        st = band_stats.get(band)
        if not st:
            continue
        band_rows.append({
            "得点帯": band,
            "出走延べ回数": st["n"],
            "1着回数": st["wins"],
            "1着率%": round(st["wins"] / st["n"] * 100, 2) if st["n"] else None,
        })

    return {
        "note": (
            "競走得点だけで着順を予測した場合と、現行モデル(blended_win_prob順位)を比較する"
            "読み取り専用診断。本番ロジックは変更していない。"
        ),
        "評価対象レース数": n_eval,
        "除外(得点データ不足)": skipped_no_score,
        "除外(結果パース不可)": skipped_no_result,
        "1着的中率比較": {
            "競走得点順位のみ%": round(score_1st_correct / n_eval * 100, 2) if n_eval else None,
            "現行モデル%": round(model_1st_correct / n_eval * 100, 2) if n_eval else None,
            "件数": n_eval,
        },
        "2着的中率比較(実際の1着を固定)": {
            "競走得点順位のみ%": round(score_2nd_correct / n_eval * 100, 2) if n_eval else None,
            "現行モデル%": round(model_2nd_correct / n_eval * 100, 2) if n_eval else None,
            "件数": n_eval,
        },
        "3着的中率比較(実際の1-2着を固定)": {
            "競走得点順位のみ%": round(score_3rd_correct / n_eval * 100, 2) if n_eval else None,
            "現行モデル%": round(model_3rd_correct / n_eval * 100, 2) if n_eval else None,
            "件数": n_eval,
        },
        "実際の1-2-3着の得点差": {
            "1着-2着_平均": _avg(diff_1_2),
            "1着-2着_中央値": _median(diff_1_2),
            "1着-3着_平均": _avg(diff_1_3),
            "1着-3着_中央値": _median(diff_1_3),
            "2着-3着_平均": _avg(diff_2_3),
            "2着-3着_中央値": _median(diff_2_3),
        },
        "得点帯別1着率": band_rows,
        "読み方": (
            "『競走得点順位のみ』が『現行モデル』を上回る、または大きく近づいていれば、"
            "競走得点を明示的に特徴量として取り込む価値が高い。"
            "下回っている場合は、Geminiが既に競走得点をblended_win_probに織り込んでいるか、"
            "得点以外の情報(ライン・脚質等)の寄与が大きいことを示す。"
            "得点帯別1着率が単調に上がっていれば、得点は有効なシグナルである。"
        ),
    }


@router.get("/race-score-band-factors")
def diagnostics_race_score_band_factors(db: Session = Depends(get_db)):
    """
    課題L続き: 競走得点帯ごとの経験的補正倍率を算出し、
    現行1着確率に掛けた場合の1着的中率がどう変わるかを遡及検証する読み取り専用診断。

    手順:
    1. 確定済みレースで、各選手の得点帯ごとの「予測1着確率合計 vs 実際の1着回数」から
       factor = actual_win_rate / predicted_avg_prob を算出（shrink付き）
    2. 同じレース群で、現行 blended_win_prob に帯別factorを掛けて再正規化した順位で
       1着的中率を再計算し、無補正と比較する

    本番ロジック(ev.py)は一切変更しない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    def _score_band(s):
        if s is None:
            return None
        if s < 90:
            return "90未満"
        if s < 95:
            return "90-95"
        if s < 100:
            return "95-100"
        if s < 105:
            return "100-105"
        if s < 110:
            return "105-110"
        return "110以上"

    # --- Pass 1: 帯別の予測確率合計と実際の1着回数 ---
    band_pred_sum = {}
    band_win_count = {}
    band_n = {}

    parsed_races = []  # (actual_1st, {car: (prob, score, band)})
    skipped = 0

    for race in races:
        entries = race.entries or []
        if len(entries) < 3:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped += 1
            continue
        actual = tuple(canonical[0][:3])
        if len(actual) < 1:
            skipped += 1
            continue
        actual_1st = actual[0]

        car_info = {}
        for e in entries:
            if e.car_number is None:
                continue
            car = int(e.car_number)
            p = e.blended_win_prob
            if p is None and e.app_win_rate is not None:
                p = e.app_win_rate / 100.0
            if p is None or p <= 0:
                continue
            sc = float(e.race_score) if e.race_score is not None else None
            band = _score_band(sc)
            if band is None:
                continue
            car_info[car] = (float(p), sc, band)

        if actual_1st not in car_info or len(car_info) < 3:
            skipped += 1
            continue

        # レース内で確率を正規化してから帯に積む
        total_p = sum(v[0] for v in car_info.values())
        if total_p <= 1e-12:
            skipped += 1
            continue
        for car, (p, sc, band) in car_info.items():
            pn = p / total_p
            band_pred_sum[band] = band_pred_sum.get(band, 0.0) + pn
            band_n[band] = band_n.get(band, 0) + 1
            if car == actual_1st:
                band_win_count[band] = band_win_count.get(band, 0) + 1

        parsed_races.append((actual_1st, car_info))

    # 帯別 factor 算出
    band_order = ["90未満", "90-95", "95-100", "100-105", "105-110", "110以上"]
    factors = {}
    factor_rows = []
    for band in band_order:
        n = band_n.get(band, 0)
        wins = band_win_count.get(band, 0)
        pred = band_pred_sum.get(band, 0.0)
        if n < 30 or pred <= 1e-9:
            factors[band] = 1.0
            factor_rows.append({
                "得点帯": band,
                "出走延べ回数": n,
                "1着回数": wins,
                "予測確率合計": round(pred, 3),
                "実績1着率%": round(wins / n * 100, 2) if n else None,
                "予測平均確率%": round(pred / n * 100, 4) if n else None,
                "raw_ratio": None,
                "補正倍率factor": 1.0,
                "備考": "サンプル不足のため補正なし",
            })
            continue
        act_rate = wins / n
        pred_avg = pred / n
        raw = act_rate / pred_avg if pred_avg > 1e-12 else 1.0
        # shrink: 期待的中数が少ないほど1.0に寄せる
        expected = pred  # 予測確率合計 = 期待的中数
        shrink = min(1.0, expected / 20.0)
        factor = 1.0 + shrink * (raw - 1.0)
        factor = max(0.5, min(2.0, factor))
        factors[band] = round(factor, 4)
        factor_rows.append({
            "得点帯": band,
            "出走延べ回数": n,
            "1着回数": wins,
            "予測確率合計": round(pred, 3),
            "実績1着率%": round(act_rate * 100, 2),
            "予測平均確率%": round(pred_avg * 100, 4),
            "raw_ratio": round(raw, 4),
            "補正倍率factor": factors[band],
            "備考": None,
        })

    # --- Pass 2: factor適用前後の1着的中率 ---
    base_correct = 0
    boosted_correct = 0
    n_eval = 0
    for actual_1st, car_info in parsed_races:
        # 無補正
        by_base = sorted(car_info.keys(), key=lambda c: car_info[c][0], reverse=True)
        if by_base[0] == actual_1st:
            base_correct += 1
        # 帯別factor適用→再正規化
        adj = {}
        for car, (p, sc, band) in car_info.items():
            adj[car] = p * factors.get(band, 1.0)
        total = sum(adj.values())
        if total > 1e-12:
            adj = {c: v / total for c, v in adj.items()}
        by_adj = sorted(adj.keys(), key=lambda c: adj[c], reverse=True)
        if by_adj[0] == actual_1st:
            boosted_correct += 1
        n_eval += 1

    return {
        "note": (
            "競走得点帯ごとの経験的補正倍率を算出し、現行1着確率に掛けた場合の"
            "1着的中率変化を遡及検証する読み取り専用診断。本番ロジックは変更していない。"
        ),
        "評価対象レース数": n_eval,
        "除外レース数": skipped,
        "得点帯別_補正倍率": factor_rows,
        "1着的中率_補正前後比較": {
            "無補正(現行順位)%": round(base_correct / n_eval * 100, 2) if n_eval else None,
            "得点帯factor適用後%": round(boosted_correct / n_eval * 100, 2) if n_eval else None,
            "差分pt": round((boosted_correct - base_correct) / n_eval * 100, 2) if n_eval else None,
            "件数": n_eval,
        },
        "読み方": (
            "『補正倍率factor』が1より大きい帯はモデルが過小評価、小さい帯は過大評価している。"
            "『1着的中率_補正前後比較』でfactor適用後が明確に上がれば、"
            "本番の1着確率に得点帯補正を入れる価値がある。"
            "差が小さい・下がる場合は、単純な帯別倍率では不十分（相対順位や得点差の方が有効）の可能性がある。"
        ),
    }


@router.get("/calibration-significance")
def diagnostics_calibration_significance(
    since: Optional[str] = Query("calibration_switch"),
    db: Session = Depends(get_db),
):
    """
    賭式・確率帯ごとの予測確率と実績的中率の乖離が統計的に有意かを確認する読み取り専用診断。
    """
    import traceback
    try:
        since_dt = None
        if since not in (None, "all", ""):
            since_dt = purchases_router._parse_since_param(since)

        q = db.query(models.Purchase).filter(
            models.Purchase.result.in_(("win", "lose"))
        ).filter(models.Purchase.bet_type == "3連単")
        if since_dt is not None:
            q = q.filter(models.Purchase.purchased_at >= since_dt)
        rows = q.all()

        def _prob_band(p: float) -> str:
            if p < 0.05:
                return "0-5%(大穴)"
            if p < 0.15:
                return "5-15%"
            if p < 0.30:
                return "15-30%"
            return "30%以上(本命)"

        def _odds_band(o: Optional[float]) -> str:
            if o is None:
                return "不明"
            if o < 5:
                return "1-5倍"
            if o < 10:
                return "5-10倍"
            if o < 30:
                return "10-30倍"
            if o < 100:
                return "30-100倍"
            if o < 300:
                return "100-300倍"
            if o < 1000:
                return "300-1000倍"
            if o < 3000:
                return "1000-3000倍"
            return "3000倍以上"

        cells = {}
        odds_cells = {}

        for pur in rows:
            try:
                pred = getattr(pur, "win_prob_raw", None)
                if pred is None:
                    pred = getattr(pur, "win_prob_at_purchase", None)
                if pred is None:
                    continue
                pred = float(pred)
                if pred <= 0:
                    continue
                if pred > 1.0:
                    pred = pred / 100.0
                if pred >= 1.0:
                    pred = 0.999999
                win = (getattr(pur, "result", None) == "win")
                bt = str(getattr(pur, "bet_type", None) or "不明")
                pb = _prob_band(pred)
                odds_val = None
                raw_odds = getattr(pur, "odds_value", None)
                if raw_odds is not None:
                    try:
                        odds_val = float(raw_odds)
                    except (TypeError, ValueError):
                        odds_val = None
                ob = _odds_band(odds_val)
                cells.setdefault((bt, pb), []).append((win, pred, odds_val))
                odds_cells.setdefault((bt, ob), []).append((win, pred, odds_val))
            except Exception:
                continue

        def _summarize(items):
            n = len(items)
            if n == 0:
                return None
            wins = sum(1 for w, _, _ in items if w)
            pred_avg = sum(p for _, p, _ in items) / n
            act = wins / n
            residual = act - pred_avg
            se = math.sqrt(max(act * (1.0 - act), 1e-12) / n)
            ci_low = max(0.0, act - 1.96 * se)
            ci_high = min(1.0, act + 1.96 * se)
            try:
                p_value = float(calc.binomial_lower_tail_p(wins, n, pred_avg))
            except Exception:
                p_value = 1.0
            over = pred_avg > act and p_value < 0.05 and n >= 30
            under = act > pred_avg and p_value > 0.95 and n >= 30
            return {
                "件数": n,
                "的中数": wins,
                "予測平均確率%": round(pred_avg * 100, 4),
                "実績的中率%": round(act * 100, 4),
                "残差pt": round(residual * 100, 4),
                "実績的中率_95CI%": [round(ci_low * 100, 4), round(ci_high * 100, 4)],
                "片側p値_過大予測%": round(p_value * 100, 6),
                "判定": (
                    "有意に過大予測" if over else
                    ("有意に過小予測の疑い" if under else
                     ("サンプル不足" if n < 30 else "有意差なし/判断保留"))
                ),
            }

        by_bet_prob = []
        for key, items in sorted(cells.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
            st = _summarize(items)
            if st:
                by_bet_prob.append({"券種": key[0], "確率帯": key[1], **st})

        by_bet_odds = []
        for key, items in sorted(odds_cells.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
            st = _summarize(items)
            if st:
                by_bet_odds.append({"券種": key[0], "オッズ帯": key[1], **st})

        notable = [r for r in by_bet_prob if r.get("判定") == "有意に過大予測"]
        notable.sort(key=lambda r: r.get("片側p値_過大予測%", 100))

        return {
            "note": "賭式×確率帯/オッズ帯の予測と実績の乖離有意性。係数は変更しない。",
            "since": since,
            "since_resolved": since_dt.isoformat() if since_dt else None,
            "対象購入数": len(rows),
            "券種×確率帯": by_bet_prob,
            "券種×オッズ帯": by_bet_odds,
            "有意に過大予測のセル": notable[:20],
            "読み方": "有意に過大予測かつn>=30は補正検討候補。自動では閾値変更しない。",
        }
    except Exception as e:
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()[-3000:],
        }

@router.get("/first-place-signal-compare")
def diagnostics_first_place_signal_compare(db: Session = Depends(get_db)):
    """1着信号比較（読み取り専用）。フィルター・購入は見ない。"""
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )
    n = 0
    hits = {
        "blended_top": 0,
        "race_score_top": 0,
        "band_adjusted_top": 0,
        "score_rank_blend_top": 0,
        "agree": 0,
        "agree_hit": 0,
    }
    skipped = 0
    for race in races:
        entries = [e for e in (race.entries or []) if e.car_number is not None]
        if len(entries) < 3:
            skipped += 1
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            skipped += 1
            continue
        actual_1st = canonical[0][0]
        blended, scores = {}, {}
        for e in entries:
            car = int(e.car_number)
            p = e.blended_win_prob if e.blended_win_prob is not None else e.ai_win_prob
            if p is None and e.app_win_rate is not None:
                p = float(e.app_win_rate) / 100.0 if float(e.app_win_rate) > 1 else float(e.app_win_rate)
            if p is not None:
                blended[car] = float(p)
            if e.race_score is not None:
                scores[car] = float(e.race_score)
        if len(blended) < 3:
            skipped += 1
            continue
        n += 1
        b_top = max(blended, key=lambda c: blended[c])
        s_top = max(scores, key=lambda c: scores[c]) if scores else None
        try:
            adjusted = calc.apply_race_score_band_factors(dict(blended), entries)
            a_top = max(adjusted, key=lambda c: adjusted[c]) if adjusted else b_top
        except Exception:
            adjusted = dict(blended)
            a_top = b_top
        try:
            mixed = calc.blend_race_score_rank_into_probs(dict(adjusted), entries)
            m_top = max(mixed, key=lambda c: mixed[c]) if mixed else a_top
        except Exception:
            m_top = a_top
        if b_top == actual_1st:
            hits["blended_top"] += 1
        if s_top is not None and s_top == actual_1st:
            hits["race_score_top"] += 1
        if a_top == actual_1st:
            hits["band_adjusted_top"] += 1
        if m_top == actual_1st:
            hits["score_rank_blend_top"] += 1
        if s_top is not None and b_top == s_top:
            hits["agree"] += 1
            if b_top == actual_1st:
                hits["agree_hit"] += 1

    def rate(k):
        return round(hits[k] / n * 100, 2) if n else None

    return {
        "note": "1着予測信号の比較。投票有無は見ない。得点ランク合成は本番_build_win_probsと同じ処理。",
        "評価レース数": n,
        "除外": skipped,
        "合成重み": getattr(calc, "RACE_SCORE_RANK_BLEND_WEIGHT", None),
        "1着的中率%": {
            "blended最大": rate("blended_top"),
            "競走得点最大": rate("race_score_top"),
            "得点帯factor適用後": rate("band_adjusted_top"),
            "得点ランク合成後": rate("score_rank_blend_top"),
        },
        "blendedと得点の一致": {
            "一致レース数": hits["agree"],
            "一致かつ1着的中率%": (
                round(hits["agree_hit"] / hits["agree"] * 100, 2) if hits["agree"] else None
            ),
        },
    }


@router.get("/order-place-signals")
def diagnostics_order_place_signals(db: Session = Depends(get_db)):
    """2着・3着の条件付き信号比較。ラインブーストなし。"""
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    def _scores(entries):
        out = {}
        for e in entries or []:
            if e.car_number is None or e.race_score is None:
                continue
            try:
                out[int(e.car_number)] = float(e.race_score)
            except (TypeError, ValueError):
                pass
        return out

    c2 = {"n": 0, "prob_top": 0, "score_top": 0, "same_line_score_top": 0,
          "same_line_n": 0, "diff_line_score_top": 0, "diff_line_n": 0, "actual_same_line": 0}
    c3 = {"n": 0, "prob_top": 0, "score_top": 0, "same_line_score_top": 0,
          "same_line_n": 0, "diff_line_score_top": 0, "diff_line_n": 0, "actual_same_line_as_1st": 0}
    skipped = 0

    for race in races:
        entries = race.entries or []
        win_probs = calc.build_win_probs_from_entries(entries)
        if not win_probs or len(win_probs) < 3:
            skipped += 1
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical or len(canonical[0]) < 3:
            skipped += 1
            continue
        a1, a2, a3 = canonical[0][0], canonical[0][1], canonical[0][2]
        if a1 not in win_probs or a2 not in win_probs or a3 not in win_probs:
            skipped += 1
            continue
        scores = _scores(entries)
        line_map, _ = calc.line_map_from_race(race)

        remain2 = {c: p for c, p in win_probs.items() if c != a1}
        if len(remain2) < 2:
            skipped += 1
            continue
        mass2 = sum(remain2.values()) or 1.0
        cond2 = {c: p / mass2 for c, p in remain2.items()}
        p2_top = max(cond2, key=lambda c: cond2[c])
        s2 = {c: scores[c] for c in remain2 if c in scores}
        s2_top = max(s2, key=lambda c: s2[c]) if s2 else None
        c2["n"] += 1
        if p2_top == a2:
            c2["prob_top"] += 1
        if s2_top is not None and s2_top == a2:
            c2["score_top"] += 1
        if line_map and line_map.get(a1) is not None:
            lid = line_map.get(a1)
            same = {c: scores[c] for c in remain2 if c in scores and line_map.get(c) == lid}
            diff = {c: scores[c] for c in remain2 if c in scores and line_map.get(c) != lid}
            if same:
                c2["same_line_n"] += 1
                if max(same, key=lambda c: same[c]) == a2:
                    c2["same_line_score_top"] += 1
            if diff:
                c2["diff_line_n"] += 1
                if max(diff, key=lambda c: diff[c]) == a2:
                    c2["diff_line_score_top"] += 1
            if line_map.get(a2) == lid:
                c2["actual_same_line"] += 1

        remain3 = {c: p for c, p in win_probs.items() if c not in (a1, a2)}
        if not remain3:
            continue
        mass3 = sum(remain3.values()) or 1.0
        cond3 = {c: p / mass3 for c, p in remain3.items()}
        p3_top = max(cond3, key=lambda c: cond3[c])
        s3 = {c: scores[c] for c in remain3 if c in scores}
        s3_top = max(s3, key=lambda c: s3[c]) if s3 else None
        c3["n"] += 1
        if p3_top == a3:
            c3["prob_top"] += 1
        if s3_top is not None and s3_top == a3:
            c3["score_top"] += 1
        if line_map and line_map.get(a1) is not None:
            lid = line_map.get(a1)
            same = {c: scores[c] for c in remain3 if c in scores and line_map.get(c) == lid}
            diff = {c: scores[c] for c in remain3 if c in scores and line_map.get(c) != lid}
            if same:
                c3["same_line_n"] += 1
                if max(same, key=lambda c: same[c]) == a3:
                    c3["same_line_score_top"] += 1
            if diff:
                c3["diff_line_n"] += 1
                if max(diff, key=lambda c: diff[c]) == a3:
                    c3["diff_line_score_top"] += 1
            if line_map.get(a3) == lid:
                c3["actual_same_line_as_1st"] += 1

    def pack(d, same_key):
        n = d["n"] or 1
        out = {
            "件数": d["n"],
            "残存確率最大の的中率%": round(d["prob_top"] / n * 100, 2) if d["n"] else None,
            "残存得点最大の的中率%": round(d["score_top"] / n * 100, 2) if d["n"] else None,
        }
        if d.get("same_line_n"):
            out["同ライン残存得点最大"] = {
                "対象レース数": d["same_line_n"],
                "的中率%": round(d["same_line_score_top"] / d["same_line_n"] * 100, 2),
            }
        if d.get("diff_line_n"):
            out["異ライン残存得点最大"] = {
                "対象レース数": d["diff_line_n"],
                "的中率%": round(d["diff_line_score_top"] / d["diff_line_n"] * 100, 2),
            }
        if d["n"]:
            out["実際が1着と同ラインだった率%"] = round(d[same_key] / d["n"] * 100, 2)
        return out

    return {
        "note": "2着=実際1着固定 / 3着=実際1-2着固定。ラインブーストなし。",
        "除外": skipped,
        "2着_1着固定時": pack(c2, "actual_same_line"),
        "3着_1着2着固定時": pack(c3, "actual_same_line_as_1st"),
    }


@router.get("/same-line-remain-2nd")
def diagnostics_same_line_remain_2nd(db: Session = Depends(get_db)):
    """2着: 同ライン残への軽い優先ルール比較。全面line_boostは使わない。"""
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )
    rules = {
        "残存確率最大": 0,
        "同ラインがいれば同ライン内の確率最大_なければ全体確率最大": 0,
        "同ライン確率を1.15倍してから最大": 0,
        "同ライン確率を1.3倍してから最大": 0,
        "同ラインがいれば同ライン内の得点最大_なければ全体確率最大": 0,
    }
    n = 0
    n_has_same = 0
    skipped = 0
    for race in races:
        entries = race.entries or []
        win_probs = calc.build_win_probs_from_entries(entries)
        if not win_probs or len(win_probs) < 3:
            skipped += 1
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            skipped += 1
            continue
        canonical = parsed.get("canonical_orderings") or []
        if not canonical or len(canonical[0]) < 2:
            skipped += 1
            continue
        a1, a2 = canonical[0][0], canonical[0][1]
        if a1 not in win_probs or a2 not in win_probs:
            skipped += 1
            continue
        remain = {c: float(p) for c, p in win_probs.items() if c != a1}
        if not remain:
            skipped += 1
            continue
        mass = sum(remain.values()) or 1.0
        cond = {c: p / mass for c, p in remain.items()}
        scores = {}
        for e in entries:
            if e.car_number is None or e.race_score is None:
                continue
            try:
                scores[int(e.car_number)] = float(e.race_score)
            except (TypeError, ValueError):
                pass
        line_map, _ = calc.line_map_from_race(race)
        lid = line_map.get(a1) if line_map else None
        same = {}
        if lid is not None and line_map:
            same = {c: cond[c] for c in cond if line_map.get(c) == lid}
        n += 1
        if same:
            n_has_same += 1
        pick_a = max(cond, key=lambda c: cond[c])
        if pick_a == a2:
            rules["残存確率最大"] += 1
        pick_b = max(same, key=lambda c: same[c]) if same else pick_a
        if pick_b == a2:
            rules["同ラインがいれば同ライン内の確率最大_なければ全体確率最大"] += 1
        for mult, name in ((1.15, "同ライン確率を1.15倍してから最大"), (1.3, "同ライン確率を1.3倍してから最大")):
            boosted = dict(cond)
            if same:
                for c in same:
                    boosted[c] = cond[c] * mult
            if max(boosted, key=lambda c: boosted[c]) == a2:
                rules[name] += 1
        if same:
            same_scores = {c: scores[c] for c in same if c in scores}
            pick_e = max(same_scores, key=lambda c: same_scores[c]) if same_scores else max(same, key=lambda c: same[c])
        else:
            pick_e = pick_a
        if pick_e == a2:
            rules["同ラインがいれば同ライン内の得点最大_なければ全体確率最大"] += 1

    def rate(hits):
        return round(hits / n * 100, 2) if n else None

    ranked = sorted(
        [{"ルール": k, "的中率%": rate(v), "的中数": v} for k, v in rules.items()],
        key=lambda x: (-(x["的中率%"] or 0), x["ルール"]),
    )
    base = rate(rules["残存確率最大"])
    best = ranked[0] if ranked else None
    delta = round(best["的中率%"] - base, 2) if best and base is not None else None
    return {
        "note": "2着・同ライン残の軽い優先比較。全面line_boostではない。",
        "評価レース数": n,
        "同ライン残があるレース数": n_has_same,
        "除外": skipped,
        "ルール別的中率": ranked,
        "ベースライン的中率%": base,
        "最良ルールとの差pt": delta,
    }

