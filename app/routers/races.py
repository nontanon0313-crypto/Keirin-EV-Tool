from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models

router = APIRouter(prefix="/races", tags=["races"])


@router.post("/{race_id}/confirm-result")
def confirm_race_result(race_id: int, actual_result: str, db: Session = Depends(get_db)):
    """
    レースの実際の着順を1回入力するだけで、そのレースに紐づく全ての未確定購入を
    自動で的中/不的中判定し、証拠金にも反映する。
    actual_result: 例 "2-5-1" (1着2番、2着5番、3着1番)
    """
    from .. import ev_calculator as calc
    from . import bankroll as bankroll_router

    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    try:
        actual_top3 = [int(x) for x in actual_result.split("-")]
    except ValueError:
        raise HTTPException(400, "着順の形式が正しくありません(例: 2-5-1)")

    race.actual_result = actual_result
    db.commit()

    pending = (
        db.query(models.Purchase)
        .filter(models.Purchase.race_id == race_id, models.Purchase.result == "pending")
        .all()
    )

    # 以前はここで購入1件ごとにdb.commit()+adjust_balance(内部でも1回commit)していたため、
    # 未確定件数が多いレースほど極端に時間がかかっていた。ここでは全件の更新を1回のcommitに
    # まとめ、証拠金の増減も合計してまとめて1回だけ反映する。
    updated = []
    total_payout_delta = 0.0
    for p in pending:
        is_win = calc.judge_purchase_result(p.bet_type, p.combination, actual_top3)
        settlement_odds = p.final_odds if p.final_odds is not None else p.odds_at_purchase
        payout = round(p.stake_amount * settlement_odds) if (is_win and settlement_odds) else 0

        p.result = "win" if is_win else "lose"
        p.payout_amount = payout
        total_payout_delta += payout

        updated.append({
            "purchase_id": p.id,
            "bet_type": p.bet_type,
            "combination": p.combination,
            "result": p.result,
            "payout_amount": payout,
            "payout_is_estimated": p.final_odds is None,
        })

    db.commit()
    if total_payout_delta:
        bankroll_router.adjust_balance(db, total_payout_delta)

    return {
        "race_id": race_id,
        "actual_result": actual_result,
        "updated_count": len(updated),
        "updated": updated,
    }


@router.get("/")
def list_races(db: Session = Depends(get_db)):
    races = db.query(models.Race).order_by(models.Race.id.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "grade": r.grade,
            "event_title": r.event_title,
            "entry_count": len(r.entries),
            "odds_count": len(r.odds_list),
        }
        for r in races
    ]


@router.get("/{race_id}")
def get_race(race_id: int, db: Session = Depends(get_db)):
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    odds_by_type = {}
    for o in race.odds_list:
        odds_by_type[o.bet_type] = odds_by_type.get(o.bet_type, 0) + 1

    return {
        "id": race.id,
        "venue_name": race.venue_name,
        "race_number": race.race_number,
        "grade": race.grade,
        "race_stage": race.race_stage,
        "weather": race.weather,
        "temperature_c": race.temperature_c,
        "season": race.season,
        "development_simulation": race.development_simulation,
        "event_title": race.event_title,
        "lines_data": race.lines_data,
        "bank_info": (
            {
                "lap_length_m": race.bank.lap_length_m,
                "home_stretch_length_m": race.bank.home_stretch_length_m,
                "lead_advantage_score": race.bank.lead_advantage_score,
            }
            if race.bank else None
        ),
        "entries": [
            {
                "car_number": e.car_number,
                "player_name": e.player_name,
                "region": e.region,
                "is_local": e.is_local,
                "leg_style": e.leg_style,
                "race_score": e.race_score,
                "app_win_rate": e.app_win_rate,
                "ai_win_prob": round(e.ai_win_prob * 100, 2) if e.ai_win_prob is not None else None,
                "blended_win_prob": round(e.blended_win_prob * 100, 2) if e.blended_win_prob is not None else None,
                "ready_for_ev": e.blended_win_prob is not None,
                "pre_race_comment": e.pre_race_comment,
            }
            for e in race.entries
        ],
        "odds_count": len(race.odds_list),
        "odds_by_type": odds_by_type,
        "ready_for_ev_calc": (
            len(race.entries) > 0
            and all(e.blended_win_prob is not None for e in race.entries)
            and len(race.odds_list) > 0
        ),
    }


@router.delete("/{race_id}")
def delete_race(race_id: int, db: Session = Depends(get_db)):
    """レースを削除する(選手・オッズ・期待値結果も連動して削除される)。誤って作成したレースのやり直し用。"""
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")
    db.delete(race)
    db.commit()
    return {"deleted": True, "race_id": race_id}
