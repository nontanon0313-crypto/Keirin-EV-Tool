from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime

from ..database import get_db
from .. import models
from ..gemini_parser import parse_screenshot, estimate_ai_win_probabilities
from ..keirin_data import is_local_player

router = APIRouter(prefix="/analyze", tags=["analyze"])


def _get_or_create_race(db: Session, parsed: dict) -> models.Race:
    venue = parsed.get("venue_name")
    race_number = parsed.get("race_number")
    query = db.query(models.Race)
    if venue:
        query = query.filter(models.Race.venue_name == venue)
    if race_number:
        query = query.filter(models.Race.race_number == race_number)
    existing = query.order_by(models.Race.id.desc()).first()
    if existing:
        # 過去に作成されたレースでbank_masterが未紐付けの場合、ここで backfill する
        if existing.bank_id is None and existing.venue_name:
            bank = db.query(models.BankMaster).filter(models.BankMaster.name == existing.venue_name).first()
            if bank:
                existing.bank_id = bank.id
                db.commit()
        return existing

    bank = None
    if venue:
        bank = db.query(models.BankMaster).filter(models.BankMaster.name == venue).first()

    race = models.Race(
        venue_name=venue or "不明",
        bank_id=bank.id if bank else None,
        race_number=race_number or 0,
        grade=parsed.get("grade"),
        event_title=parsed.get("event_title"),
        source_app=parsed.get("source_app"),
    )
    db.add(race)
    db.commit()
    db.refresh(race)
    return race


def _upsert_entries(db: Session, race: models.Race, entries: list):
    for e in entries:
        car_number = e.get("car_number")
        if car_number is None:
            continue
        existing = (
            db.query(models.Entry)
            .filter(models.Entry.race_id == race.id, models.Entry.car_number == car_number)
            .first()
        )
        fields = {
            "waku_number": e.get("waku_number"),
            "player_name": e.get("player_name") or (existing.player_name if existing else "不明"),
            "region": e.get("region"),
            "player_class": e.get("player_class"),
            "age": e.get("age"),
            "period": e.get("period"),
            "evaluation_rank": e.get("evaluation_rank"),
            "race_score": e.get("race_score"),
            "leg_style": e.get("leg_style"),
            "s_count": e.get("s_count"),
            "h_count": e.get("h_count"),
            "b_count": e.get("b_count"),
            "kimarite_nige": e.get("kimarite_nige"),
            "kimarite_makuri": e.get("kimarite_makuri"),
            "kimarite_sashi": e.get("kimarite_sashi"),
            "kimarite_mark": e.get("kimarite_mark"),
            "finish_1st": e.get("finish_1st"),
            "finish_2nd": e.get("finish_2nd"),
            "finish_3rd": e.get("finish_3rd"),
            "app_win_rate": e.get("app_win_rate"),
            "app_2nd_rate": e.get("app_2nd_rate"),
            "app_3rd_rate": e.get("app_3rd_rate"),
            "gear_ratio": e.get("gear_ratio"),
            "line_group": e.get("line_group"),
        }
        region = e.get("region")
        if region:
            fields["is_local"] = is_local_player(race.venue_name, region)
        # Noneで既存の値を上書きしないようにする(複数画像から段階的に情報を埋めるため)
        if existing:
            for k, v in fields.items():
                if v is not None:
                    setattr(existing, k, v)
        else:
            new_entry = models.Entry(race_id=race.id, car_number=car_number, **fields)
            db.add(new_entry)
    db.commit()


def _upsert_odds(db: Session, race: models.Race, odds_list: list):
    for o in odds_list:
        bet_type = o.get("bet_type")
        combination = o.get("combination")
        odds_value = o.get("odds_value")
        if not bet_type or not combination or odds_value is None:
            continue
        existing = (
            db.query(models.Odds)
            .filter(
                models.Odds.race_id == race.id,
                models.Odds.bet_type == bet_type,
                models.Odds.combination == combination,
            )
            .first()
        )
        if existing:
            existing.odds_value = odds_value
            existing.popularity_rank = o.get("popularity_rank")
            if o.get("total_vote_amount") is not None:
                existing.total_vote_amount = o.get("total_vote_amount")
            existing.updated_at = datetime.utcnow()
        else:
            db.add(
                models.Odds(
                    race_id=race.id,
                    bet_type=bet_type,
                    combination=combination,
                    odds_value=odds_value,
                    popularity_rank=o.get("popularity_rank"),
                    total_vote_amount=o.get("total_vote_amount"),
                )
            )
    db.commit()


@router.post("/screenshots")
async def analyze_screenshots(files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    """
    複数枚のスクショ(出走表・成績・勝率・オッズを問わず)をまとめてアップロードし、
    Geminiで解析してDBに反映する。
    """
    results = []
    affected_races = {}

    for f in files:
        content = await f.read()
        mime = f.content_type or "image/png"
        parsed = parse_screenshot(content, mime)
        race = _get_or_create_race(db, parsed)
        affected_races[race.id] = race

        if parsed.get("entries"):
            _upsert_entries(db, race, parsed["entries"])
        if parsed.get("odds_list"):
            _upsert_odds(db, race, parsed["odds_list"])
        if parsed.get("lines"):
            race.lines_data = parsed["lines"]
            db.commit()

        results.append({
            "filename": f.filename,
            "screen_type": parsed.get("screen_type"),
            "race_id": race.id,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
            "entries_found": len(parsed.get("entries") or []),
            "odds_found": len(parsed.get("odds_list") or []),
        })

    # 全画像取り込み後、レースごとにAI独自の勝率推定を実行
    for race_id, race in affected_races.items():
        entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
        if not entries:
            continue
        entries_payload = [
            {
                "car_number": e.car_number,
                "player_name": e.player_name,
                "race_score": e.race_score,
                "leg_style": e.leg_style,
                "s_count": e.s_count,
                "h_count": e.h_count,
                "b_count": e.b_count,
                "kimarite_nige": e.kimarite_nige,
                "kimarite_makuri": e.kimarite_makuri,
                "kimarite_sashi": e.kimarite_sashi,
                "kimarite_mark": e.kimarite_mark,
                "finish_1st": e.finish_1st,
                "finish_2nd": e.finish_2nd,
                "finish_3rd": e.finish_3rd,
                "line_group": e.line_group,
                "app_win_rate": e.app_win_rate,
                "is_local": e.is_local,
            }
            for e in entries
        ]
        try:
            bank = race.bank
            bank_info = None
            if bank:
                bank_info = {
                    "lap_length_m": bank.lap_length_m,
                    "home_stretch_length_m": bank.home_stretch_length_m,
                    "lead_advantage_score": bank.lead_advantage_score,
                }
            ai_probs = estimate_ai_win_probabilities(entries_payload, lines=race.lines_data, bank_info=bank_info)
        except Exception:
            ai_probs = {}

        for e in entries:
            ai_p = ai_probs.get(e.car_number)
            if ai_p is not None:
                e.ai_win_prob = ai_p
            app_p = (e.app_win_rate / 100.0) if e.app_win_rate is not None else None
            if ai_p is not None and app_p is not None:
                e.blended_win_prob = (ai_p + app_p) / 2.0
            elif ai_p is not None:
                e.blended_win_prob = ai_p
            elif app_p is not None:
                e.blended_win_prob = app_p
        db.commit()

    return {"processed_files": len(files), "results": results, "race_ids": list(affected_races.keys())}
