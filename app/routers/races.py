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
    return {
        "id": race.id,
        "venue_name": race.venue_name,
        "race_number": race.race_number,
        "grade": race.grade,
        "event_title": race.event_title,
        "entries": [
            {
                "car_number": e.car_number,
                "player_name": e.player_name,
                "leg_style": e.leg_style,
                "race_score": e.race_score,
                "app_win_rate": e.app_win_rate,
                "ai_win_prob": round(e.ai_win_prob * 100, 2) if e.ai_win_prob is not None else None,
                "blended_win_prob": round(e.blended_win_prob * 100, 2) if e.blended_win_prob is not None else None,
            }
            for e in race.entries
        ],
        "odds_count": len(race.odds_list),
    }
