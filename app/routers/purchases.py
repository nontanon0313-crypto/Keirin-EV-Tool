from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional

from ..database import get_db
from .. import models, schemas
from . import bankroll as bankroll_router

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
    db.commit()
    # 払戻があれば証拠金残高に加算する(負けの場合はpayout_amount=0なので変化なし)
    bankroll_router.adjust_balance(db, update.payout_amount)
    return obj


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
                "expectancy_pct": round(expectancy, 2),
            }
        return out

    def prob_bucket(p):
        prob = (p.win_prob_at_purchase or 0) * 100
        if prob < 5:
            return "0-5%(大穴)"
        elif prob < 15:
            return "5-15%"
        elif prob < 30:
            return "15-30%"
        else:
            return "30%以上(本命)"

    return {
        "overall_expectancy_pct": round(overall_expectancy_pct, 2),
        "total_bets": len(purchases),
        "by_bet_type": bucket_stats(lambda p: p.bet_type),
        "by_win_prob_bucket": bucket_stats(prob_bucket),
        "by_bank": bucket_stats(lambda p: (p.tags or {}).get("bank", "不明")),
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
