"""
実資金の収益管理API。

Purchase / SkippedBet / 検証タブの集計とは完全に分離する。
投票プランの想定値と、実際に投票した結果を別フィールドで保持し、
実績データのみを基準に集計・資産推移を返す。
"""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas

router = APIRouter(prefix="/revenue", tags=["revenue"])


def _race_meta(db: Session, race_id: int) -> dict:
    race = db.query(models.Race).filter(models.Race.id == race_id).first()
    if not race:
        raise HTTPException(status_code=404, detail=f"race_id={race_id} not found")
    return {
        "race_id": race.id,
        "race_date": race.race_date,
        "venue_name": race.venue_name,
        "race_number": race.race_number,
    }


def _row_to_dict(row: models.LiveBet) -> dict:
    planned_stake = row.planned_stake or 0.0
    planned_prob = row.planned_win_prob
    planned_odds = row.planned_odds
    planned_ev = row.planned_ev_pct
    planned_exp = row.planned_expected_profit
    if planned_exp is None and planned_stake and planned_prob is not None and planned_odds is not None:
        planned_exp = planned_stake * (planned_prob * planned_odds - 1.0)

    actual_stake = row.actual_stake if row.actual_stake is not None else None
    actual_payout = row.actual_payout if row.actual_payout is not None else 0.0
    actual_pnl = None
    if row.vote_status == "voted" and actual_stake is not None:
        actual_pnl = actual_payout - actual_stake
    elif row.vote_status == "not_voted":
        actual_pnl = 0.0

    return {
        "id": row.id,
        "race_id": row.race_id,
        "race_date": row.race_date.isoformat() if row.race_date else None,
        "venue_name": row.venue_name,
        "race_number": row.race_number,
        "bet_type": row.bet_type,
        "combination": row.combination,
        "planned_stake": planned_stake if row.planned_stake is not None else None,
        "planned_win_prob": planned_prob,
        "planned_odds": planned_odds,
        "planned_ev_pct": planned_ev,
        "planned_expected_profit": round(planned_exp, 2) if planned_exp is not None else None,
        "vote_status": row.vote_status,
        "actual_stake": actual_stake,
        "actual_result": row.actual_result,
        "actual_payout": actual_payout,
        "actual_pnl": round(actual_pnl, 2) if actual_pnl is not None else None,
        "memo": row.memo,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/from-plan")
def register_from_plan(payload: schemas.LiveBetFromPlanCreate, db: Session = Depends(get_db)):
    """
    投票プランを収益管理へ記録。
    mark_as_voted=True(既定): プラン通り購入前提で実額=予定額・voted。
    的中時だけ払戻を書き、残りは mark-pending-lose で一括 lose。
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="items is empty")
    meta = _race_meta(db, payload.race_id)
    created = []
    mark_voted = bool(payload.mark_as_voted)
    for it in payload.items:
        planned_exp = it.planned_expected_profit
        if planned_exp is None and it.planned_stake and it.planned_win_prob is not None and it.planned_odds is not None:
            planned_exp = it.planned_stake * (it.planned_win_prob * it.planned_odds - 1.0)
        row = models.LiveBet(
            race_id=meta["race_id"],
            race_date=meta["race_date"],
            venue_name=meta["venue_name"],
            race_number=meta["race_number"],
            bet_type=it.bet_type,
            combination=it.combination,
            planned_stake=it.planned_stake,
            planned_win_prob=it.planned_win_prob,
            planned_odds=it.planned_odds,
            planned_ev_pct=it.planned_ev_pct,
            planned_expected_profit=planned_exp,
            vote_status="voted" if mark_voted else "planned",
            actual_stake=(it.planned_stake if mark_voted else None),
            actual_result="pending",
            actual_payout=0.0,
            source="from_plan",
        )
        db.add(row)
        created.append(row)
    db.commit()
    for r in created:
        db.refresh(r)
    return {
        "created_count": len(created),
        "race_id": payload.race_id,
        "mark_as_voted": mark_voted,
        "items": [_row_to_dict(r) for r in created],
    }


@router.post("/mark-pending-lose")
def mark_pending_lose(
    race_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """pending の投票済み行を一括 lose(払戻0)。的中入力後の残り処理用。"""
    q = db.query(models.LiveBet).filter(
        models.LiveBet.vote_status == "voted",
        models.LiveBet.actual_result == "pending",
    )
    if race_id is not None:
        q = q.filter(models.LiveBet.race_id == race_id)
    rows = q.all()
    now = datetime.utcnow()
    for r in rows:
        r.actual_result = "lose"
        if r.actual_payout is None:
            r.actual_payout = 0.0
        if r.actual_stake is None and r.planned_stake is not None:
            r.actual_stake = r.planned_stake
        r.updated_at = now
    db.commit()
    return {"updated_count": len(rows), "race_id": race_id}


@router.post("/{live_bet_id}/win")
def mark_win(live_bet_id: int, payload: schemas.LiveBetWinUpdate, db: Session = Depends(get_db)):
    """的中1件。払戻だけ書いて win。実額未設定なら予定額。"""
    row = db.query(models.LiveBet).filter(models.LiveBet.id == live_bet_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="live_bet not found")
    row.vote_status = "voted"
    if payload.actual_stake is not None:
        row.actual_stake = payload.actual_stake
    elif row.actual_stake is None and row.planned_stake is not None:
        row.actual_stake = row.planned_stake
    row.actual_result = "win"
    row.actual_payout = payload.actual_payout
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


@router.post("/manual")
def create_manual(payload: schemas.LiveBetManualCreate, db: Session = Depends(get_db)):
    """プランにない買い目を実績として追加する。"""
    race_date = payload.race_date
    venue_name = payload.venue_name
    race_number = payload.race_number
    if payload.race_id:
        meta = _race_meta(db, payload.race_id)
        race_date = race_date or meta["race_date"]
        venue_name = venue_name or meta["venue_name"]
        race_number = race_number if race_number is not None else meta["race_number"]

    planned_exp = None
    if payload.planned_stake and payload.planned_win_prob is not None and payload.planned_odds is not None:
        planned_exp = payload.planned_stake * (payload.planned_win_prob * payload.planned_odds - 1.0)

    row = models.LiveBet(
        race_id=payload.race_id,
        race_date=race_date,
        venue_name=venue_name,
        race_number=race_number,
        bet_type=payload.bet_type,
        combination=payload.combination,
        planned_stake=payload.planned_stake,
        planned_win_prob=payload.planned_win_prob,
        planned_odds=payload.planned_odds,
        planned_ev_pct=payload.planned_ev_pct,
        planned_expected_profit=planned_exp,
        vote_status=payload.vote_status or "voted",
        actual_stake=payload.actual_stake,
        actual_result=payload.actual_result or "pending",
        actual_payout=payload.actual_payout or 0.0,
        memo=payload.memo,
        source="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


@router.put("/{live_bet_id}")
def update_live_bet(live_bet_id: int, payload: schemas.LiveBetUpdate, db: Session = Depends(get_db)):
    row = db.query(models.LiveBet).filter(models.LiveBet.id == live_bet_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="live_bet not found")

    if payload.vote_status is not None:
        if payload.vote_status not in ("planned", "voted", "not_voted"):
            raise HTTPException(status_code=400, detail="invalid vote_status")
        row.vote_status = payload.vote_status
        if payload.vote_status == "not_voted":
            row.actual_result = "not_voted"
            if payload.actual_stake is None and row.actual_stake is None:
                row.actual_stake = 0.0
            if payload.actual_payout is None:
                row.actual_payout = 0.0

    if payload.actual_stake is not None:
        row.actual_stake = payload.actual_stake
    if payload.actual_result is not None:
        if payload.actual_result not in ("pending", "win", "lose", "not_voted"):
            raise HTTPException(status_code=400, detail="invalid actual_result")
        row.actual_result = payload.actual_result
    if payload.actual_payout is not None:
        row.actual_payout = payload.actual_payout
    if payload.memo is not None:
        row.memo = payload.memo

    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


@router.delete("/{live_bet_id}")
def delete_live_bet(live_bet_id: int, db: Session = Depends(get_db)):
    row = db.query(models.LiveBet).filter(models.LiveBet.id == live_bet_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="live_bet not found")
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": live_bet_id}


@router.get("/list")
def list_live_bets(
    race_id: Optional[int] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    q = db.query(models.LiveBet)
    if race_id is not None:
        q = q.filter(models.LiveBet.race_id == race_id)
    rows = q.order_by(models.LiveBet.created_at.asc(), models.LiveBet.id.asc()).limit(limit).all()
    return {"count": len(rows), "items": [_row_to_dict(r) for r in rows]}



def _iter_sorted(db: Session) -> List[models.LiveBet]:
    return (
        db.query(models.LiveBet)
        .order_by(models.LiveBet.race_date.asc().nullslast(), models.LiveBet.created_at.asc(), models.LiveBet.id.asc())
        .all()
    )


@router.get("/stats")
def revenue_stats(db: Session = Depends(get_db)):
    """実績基準の集計 + 想定との比較。Purchaseは一切参照しない。"""
    rows = _iter_sorted(db)

    planned_stake_sum = 0.0
    planned_exp_sum = 0.0
    planned_hit_weight = 0.0
    planned_count = 0

    actual_stake_sum = 0.0
    actual_payout_sum = 0.0
    actual_pnl_sum = 0.0
    voted_count = 0
    hit_count = 0
    not_voted_count = 0
    pending_count = 0

    for r in rows:
        if r.planned_stake is not None:
            planned_stake_sum += r.planned_stake
            planned_count += 1
            if r.planned_win_prob is not None:
                planned_hit_weight += r.planned_win_prob
            if r.planned_expected_profit is not None:
                planned_exp_sum += r.planned_expected_profit
            elif r.planned_stake and r.planned_win_prob is not None and r.planned_odds is not None:
                planned_exp_sum += r.planned_stake * (r.planned_win_prob * r.planned_odds - 1.0)

        if r.vote_status == "not_voted":
            not_voted_count += 1
            continue
        if r.vote_status != "voted":
            if r.actual_result == "pending":
                pending_count += 1
            continue

        stake = r.actual_stake if r.actual_stake is not None else 0.0
        payout = r.actual_payout if r.actual_payout is not None else 0.0
        actual_stake_sum += stake
        actual_payout_sum += payout
        actual_pnl_sum += payout - stake
        voted_count += 1
        if r.actual_result == "win":
            hit_count += 1
        elif r.actual_result == "pending":
            pending_count += 1

    planned_hit_rate = (planned_hit_weight / planned_count * 100) if planned_count else None
    planned_roi = ((planned_exp_sum / planned_stake_sum) * 100 + 100) if planned_stake_sum > 0 else None
    actual_hit_rate = (hit_count / voted_count * 100) if voted_count else None
    actual_roi = (actual_payout_sum / actual_stake_sum * 100) if actual_stake_sum > 0 else None

    def _diff(a, b):
        if a is None or b is None:
            return None
        return round(b - a, 2)

    return {
        "total_rows": len(rows),
        "planned": {
            "stake": round(planned_stake_sum, 0),
            "expected_profit": round(planned_exp_sum, 0),
            "hit_rate_pct": round(planned_hit_rate, 2) if planned_hit_rate is not None else None,
            "roi_pct": round(planned_roi, 2) if planned_roi is not None else None,
            "count": planned_count,
        },
        "actual": {
            "stake": round(actual_stake_sum, 0),
            "payout": round(actual_payout_sum, 0),
            "pnl": round(actual_pnl_sum, 0),
            "hit_rate_pct": round(actual_hit_rate, 2) if actual_hit_rate is not None else None,
            "roi_pct": round(actual_roi, 2) if actual_roi is not None else None,
            "voted_count": voted_count,
            "hit_count": hit_count,
            "not_voted_count": not_voted_count,
            "pending_count": pending_count,
        },
        "diff": {
            "stake": _diff(planned_stake_sum, actual_stake_sum),
            "pnl": _diff(planned_exp_sum, actual_pnl_sum),
            "hit_rate_pct": _diff(planned_hit_rate, actual_hit_rate),
            "roi_pct": _diff(planned_roi, actual_roi),
        },
    }


@router.get("/equity-curve")
def equity_curve(db: Session = Depends(get_db)):
    """
    横軸=累計投資額、縦軸=累積損益。
    実績系列と、想定系列(プランの期待利益を累積)を返す。
    """
    rows = _iter_sorted(db)
    points_actual = [{"cum_stake": 0.0, "cum_pnl": 0.0, "assets": 0.0}]
    points_planned = [{"cum_stake": 0.0, "cum_pnl": 0.0}]

    cum_stake_a = 0.0
    cum_pnl_a = 0.0
    cum_stake_p = 0.0
    cum_pnl_p = 0.0

    for r in rows:
        # 想定
        if r.planned_stake is not None and r.planned_stake > 0:
            exp = r.planned_expected_profit
            if exp is None and r.planned_win_prob is not None and r.planned_odds is not None:
                exp = r.planned_stake * (r.planned_win_prob * r.planned_odds - 1.0)
            exp = exp or 0.0
            cum_stake_p += r.planned_stake
            cum_pnl_p += exp
            points_planned.append({
                "cum_stake": round(cum_stake_p, 0),
                "cum_pnl": round(cum_pnl_p, 0),
                "id": r.id,
                "label": f"{r.venue_name or ''} {r.race_number or ''}R {r.bet_type} {r.combination}",
            })

        # 実績(投票したもののみ)
        if r.vote_status == "voted" and r.actual_stake is not None:
            stake = r.actual_stake
            payout = r.actual_payout if r.actual_payout is not None else 0.0
            # pendingでも投資額は計上(未確定の払戻は0扱い)
            cum_stake_a += stake
            cum_pnl_a += payout - stake
            points_actual.append({
                "cum_stake": round(cum_stake_a, 0),
                "cum_pnl": round(cum_pnl_a, 0),
                "assets": round(cum_pnl_a, 0),
                "id": r.id,
                "result": r.actual_result,
                "label": f"{r.venue_name or ''} {r.race_number or ''}R {r.bet_type} {r.combination}",
            })

    return {
        "actual": points_actual,
        "planned": points_planned,
        "final_actual_pnl": round(cum_pnl_a, 0),
        "final_actual_stake": round(cum_stake_a, 0),
        "final_assets": round(cum_pnl_a, 0),
    }
