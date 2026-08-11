from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional

from ..database import get_db
from .. import models, schemas
from . import bankroll as bankroll_router
from .. import ev_calculator as calc

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.post("/")
def create_purchase(purchase: schemas.PurchaseCreate, db: Session = Depends(get_db)):
    obj = models.Purchase(**purchase.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    # 購入した分だけ証拠金残高を減算する
    bankroll_router.adjust_balance(db, -purchase.stake_amount)
    return obj


@router.put("/{purchase_id}/result")
def update_purchase_result(purchase_id: int, update: schemas.PurchaseResultUpdate, db: Session = Depends(get_db)):
    obj = db.query(models.Purchase).get(purchase_id)
    if not obj:
        raise HTTPException(404, "購入履歴が見つかりません")
    if obj.result != "pending":
        raise HTTPException(400, "この購入履歴はすでに結果が確定しています(二重加算を防ぐため再更新できません)")
    obj.result = update.result
    obj.payout_amount = update.payout_amount
    obj.final_odds = update.final_odds
    db.commit()
    # 払戻があれば証拠金残高に加算する(負けの場合はpayout_amount=0なので変化なし)
    bankroll_router.adjust_balance(db, update.payout_amount)
    return obj


def get_calibration_factors(db: Session) -> dict:
    """
    勝率帯ごとの自動補正係数を計算する。
    試行数が「200÷帯の代表確率」に達した帯だけ、実績に基づく補正係数を返す。
    達していない帯はfactor=1.0(補正なし)のまま。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    result = {}
    for lo, hi, name, mid in calc.PROB_BUCKETS:
        bucket_purchases = [p for p in purchases if p.win_prob_at_purchase is not None and lo <= p.win_prob_at_purchase < hi]
        count = len(bucket_purchases)
        required = calc.required_sample_size(mid)
        is_reliable = count >= required

        if count > 0:
            wins = sum(1 for p in bucket_purchases if p.result == "win")
            actual_win_rate = wins / count
            predicted_avg = sum(p.win_prob_at_purchase for p in bucket_purchases) / count
        else:
            actual_win_rate = None
            predicted_avg = None

        factor = 1.0
        if is_reliable and predicted_avg:
            factor = calc.compute_calibration_factor(actual_win_rate, predicted_avg)

        result[name] = {
            "sample_count": count,
            "required_sample_count": required,
            "is_reliable": is_reliable,
            "actual_win_rate_pct": round(actual_win_rate * 100, 2) if actual_win_rate is not None else None,
            "predicted_avg_prob_pct": round(predicted_avg * 100, 2) if predicted_avg is not None else None,
            "calibration_factor": round(factor, 3),
        }
    return result


@router.get("/suggested-margin")
def suggested_margin(db: Session = Depends(get_db)):
    """
    実績のオッズ変動(投票時→最終オッズのズレ)から、安全マージンの目安を自動算出する。
    データが少ない場合はデフォルト値(5%)を返す。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    drift_info = _odds_drift_stats(purchases)

    default_margin = 5.0
    min_sample_for_trust = 10

    if drift_info.get("message") or drift_info.get("sample_count", 0) < min_sample_for_trust:
        return {
            "suggested_margin_pct": default_margin,
            "based_on_actual_data": False,
            "reason": f"実績データが不足しています({drift_info.get('sample_count', 0)}件、{min_sample_for_trust}件以上で自動算出に切り替わります)。デフォルト値を使用してください。",
        }

    avg_drift = drift_info["avg_odds_drift_pct"]
    # 不利方向(オッズが下がる)のブレ幅をそのまま安全マージンとして使う。有利方向のブレならデフォルト値を維持。
    if avg_drift < 0:
        suggested = max(default_margin, abs(avg_drift))
    else:
        suggested = default_margin

    return {
        "suggested_margin_pct": round(suggested, 1),
        "based_on_actual_data": True,
        "sample_count": drift_info["sample_count"],
        "avg_odds_drift_pct": avg_drift,
        "reason": f"実績{drift_info['sample_count']}件のオッズ変動(平均{avg_drift}%)から算出しました。",
    }


@router.get("/calibration")
def calibration_status(db: Session = Depends(get_db)):
    """勝率帯ごとの自動補正の状態(補正係数・信頼できるか・必要試行数)を返す。"""
    return get_calibration_factors(db)


@router.get("/pending")
def list_pending_purchases(db: Session = Depends(get_db)):
    """まだ結果未確定の購入履歴一覧(結果入力画面用)。レースごとにまとめられるよう、レース情報も付与する。"""
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result == "pending")
        .order_by(models.Purchase.purchased_at.desc())
        .all()
    )
    result = []
    for p in purchases:
        race = db.query(models.Race).get(p.race_id)
        result.append({
            "id": p.id,
            "race_id": p.race_id,
            "venue_name": race.venue_name if race else "不明",
            "race_number": race.race_number if race else None,
            "bet_type": p.bet_type,
            "combination": p.combination,
            "stake_amount": p.stake_amount,
            "odds_at_purchase": p.odds_at_purchase,
        })
    return result


@router.get("/")
def list_purchases(race_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(models.Purchase)
    if race_id:
        q = q.filter(models.Purchase.race_id == race_id)
    return q.order_by(models.Purchase.purchased_at.desc()).all()


@router.get("/stats")
def purchase_stats(db: Session = Depends(get_db)):
    """
    勝率帯別・券種別の回収率など、複数の切り口で集計する。
    単一要素だけで結論づけないためのFX版ルールを踏襲。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    if not purchases:
        return {"message": "まだ確定した購入履歴がありません"}

    total_stake = sum(p.stake_amount for p in purchases)
    total_payout = sum(p.payout_amount for p in purchases)
    overall_expectancy_pct = ((total_payout - total_stake) / total_stake * 100) if total_stake else 0

    def bucket_stats(key_fn):
        buckets = {}
        for p in purchases:
            key = key_fn(p)
            b = buckets.setdefault(key, {"stake": 0.0, "payout": 0.0, "count": 0, "wins": 0})
            b["stake"] += p.stake_amount
            b["payout"] += p.payout_amount
            b["count"] += 1
            if p.result == "win":
                b["wins"] += 1
        out = {}
        for k, v in buckets.items():
            expectancy = ((v["payout"] - v["stake"]) / v["stake"] * 100) if v["stake"] else 0
            out[k] = {
                "count": v["count"],
                "win_rate_pct": round(v["wins"] / v["count"] * 100, 1),
                # roi_pct: 回収率(100%が損益分岐点)。expectancy_pct: 同じ値を「0%が損益分岐点」の表現にしたもの。
                "roi_pct": round(expectancy + 100, 2),
                "expectancy_pct": round(expectancy, 2),
            }
        # 期待値(実績)が高い順に並べ替える
        return dict(sorted(out.items(), key=lambda item: -item[1]["expectancy_pct"]))

    def prob_bucket(p):
        prob = p.win_prob_at_purchase or 0
        name, _ = calc.get_prob_bucket(prob)
        return name

    def line_bucket(p):
        race = db.query(models.Race).get(p.race_id)
        if not race or not race.lines_data:
            return "ライン情報なし"
        line_map = {}
        for idx, line in enumerate(race.lines_data):
            for car in line:
                line_map[int(car)] = idx
        try:
            cars = [int(x) for x in p.combination.split("-")]
        except ValueError:
            return "ライン情報なし"
        line_ids = [line_map.get(c) for c in cars]
        if any(lid is None for lid in line_ids):
            return "ライン情報なし"
        return "同ライン絡み" if len(set(line_ids)) == 1 else "異なるライン混在"

    def bank_lead_bucket(p):
        race = db.query(models.Race).get(p.race_id)
        if not race or not race.bank or race.bank.lead_advantage_score is None:
            return "バンク情報なし"
        score = race.bank.lead_advantage_score
        if score >= 0.66:
            return "先行有利バンク(直線短め)"
        elif score >= 0.33:
            return "標準的なバンク"
        else:
            return "差し有利バンク(直線長め)"

    def race_stage_bucket(p):
        race = db.query(models.Race).get(p.race_id)
        if not race or not race.race_stage:
            return "不明"
        return race.race_stage

    def season_bucket(p):
        race = db.query(models.Race).get(p.race_id)
        if not race or not race.season:
            return "不明"
        return race.season

    def grade_bucket(p):
        race = db.query(models.Race).get(p.race_id)
        if not race or not race.grade:
            return "不明"
        return race.grade

    all_buckets = {
        "券種別": bucket_stats(lambda p: p.bet_type),
        "勝率帯別": bucket_stats(prob_bucket),
        "バンク別": bucket_stats(lambda p: (p.tags or {}).get("bank", "不明")),
        "ライン絡み別": bucket_stats(line_bucket),
        "バンク先行有利度別": bucket_stats(bank_lead_bucket),
        "レースステージ別": bucket_stats(race_stage_bucket),
        "季節別": bucket_stats(season_bucket),
        "グレード別": bucket_stats(grade_bucket),
    }

    # 全ての切り口を横断し、サンプル数が一定以上(ノイズ除け)で期待値実績が高い条件をランキング化する。
    # これが「集計結果を見て予想を修正する」ための最初の手がかりになる。
    min_sample_for_ranking = 5
    ranking = []
    for category, buckets in all_buckets.items():
        for key, v in buckets.items():
            if v["count"] >= min_sample_for_ranking:
                ranking.append({
                    "category": category,
                    "condition": key,
                    "count": v["count"],
                    "win_rate_pct": v["win_rate_pct"],
                    "expectancy_pct": v["expectancy_pct"],
                })
    ranking.sort(key=lambda x: -x["expectancy_pct"])

    return {
        "overall_expectancy_pct": round(overall_expectancy_pct, 2),
        "overall_roi_pct": round(overall_expectancy_pct + 100, 2),
        "total_bets": len(purchases),
        "best_conditions_ranking": ranking[:10],
        "worst_conditions_ranking": ranking[-10:][::-1] if len(ranking) > 10 else [],
        "by_bet_type": all_buckets["券種別"],
        "by_win_prob_bucket": all_buckets["勝率帯別"],
        "by_bank": all_buckets["バンク別"],
        "by_line_match": all_buckets["ライン絡み別"],
        "by_bank_lead_advantage": all_buckets["バンク先行有利度別"],
        "by_race_stage": all_buckets["レースステージ別"],
        "by_season": all_buckets["季節別"],
        "by_grade": all_buckets["グレード別"],
        "odds_drift": _odds_drift_stats(purchases),
        "note": (
            "expectancy_pctは0%が損益分岐点(roi_pctは100%が損益分岐点、同じ実績を別表現にしたもの)。"
            f"件数{min_sample_for_ranking}件未満の条件はランキングから除外しています(判断が不安定なため)。"
        ),
    }


def _odds_drift_stats(purchases):
    """
    ①的中率の精度とは別に、②投票時オッズ→最終オッズのズレだけを検証する。
    (最終オッズが未記録の購入は対象外)
    """
    with_final = [p for p in purchases if p.final_odds is not None and p.odds_at_purchase]
    if not with_final:
        return {"message": "最終オッズが記録された購入がまだありません"}

    drifts = [
        (p.final_odds - p.odds_at_purchase) / p.odds_at_purchase * 100
        for p in with_final
    ]
    avg_drift_pct = sum(drifts) / len(drifts)
    worsened_count = sum(1 for d in drifts if d < 0)  # オッズが下がる=自分に不利な方向

    return {
        "sample_count": len(with_final),
        "avg_odds_drift_pct": round(avg_drift_pct, 2),
        "worsened_ratio_pct": round(worsened_count / len(with_final) * 100, 1),
        "note": "マイナスは投票時より最終オッズが下がった(不利な方向に動いた)ことを意味します",
    }


@router.post("/skipped")
def record_skipped(
    race_id: int,
    bet_type: str,
    combination: str,
    win_prob_estimated: float,
    ev_pct_estimated: float,
    reason: str,
    db: Session = Depends(get_db),
):
    """見送った買い目を記録する。結果が判明したら別途PATCHで actual_result を埋める運用。"""
    obj = models.SkippedBet(
        race_id=race_id,
        bet_type=bet_type,
        combination=combination,
        win_prob_estimated=win_prob_estimated,
        ev_pct_estimated=ev_pct_estimated,
        reason=reason,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/skipped/{skipped_id}/result")
def update_skipped_result(skipped_id: int, actual_result: str, actual_payout: float = 0, db: Session = Depends(get_db)):
    obj = db.query(models.SkippedBet).get(skipped_id)
    if not obj:
        raise HTTPException(404, "見送り記録が見つかりません")
    obj.actual_result = actual_result
    obj.actual_payout = actual_payout
    db.commit()
    return obj


@router.get("/skipped/stats")
def skipped_stats(db: Session = Depends(get_db)):
    """見送りが正しかったか(機会損失/機会回避)を集計する。"""
    skipped = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None)).all()
    if not skipped:
        return {"message": "結果判明済みの見送り記録がまだありません"}

    correct_skips = sum(1 for s in skipped if s.actual_result == "lose")
    missed_opportunities = [s for s in skipped if s.actual_result == "win"]
    missed_profit = sum((s.actual_payout or 0) for s in missed_opportunities)

    return {
        "total_skipped_evaluated": len(skipped),
        "correct_skip_pct": round(correct_skips / len(skipped) * 100, 1),
        "missed_opportunities_count": len(missed_opportunities),
        "missed_profit_total": missed_profit,
    }
