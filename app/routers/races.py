from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models

router = APIRouter(prefix="/races", tags=["races"])


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
