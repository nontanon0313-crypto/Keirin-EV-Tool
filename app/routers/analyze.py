from fastapi import APIRouter, Depends, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import asyncio

from ..database import get_db
from .. import models
from ..gemini_parser import parse_screenshot, estimate_ai_win_probabilities, simulate_race_development
from ..keirin_data import is_local_player
from . import purchases as purchases_router

router = APIRouter(prefix="/analyze", tags=["analyze"])


def _current_season_jst() -> str:
    """現在時刻(UTC+9換算)の月から季節を判定する。"""
    jst_hour_offset = 9
    now = datetime.utcnow().timestamp() + jst_hour_offset * 3600
    month = datetime.utcfromtimestamp(now).month
    if month in (3, 4, 5):
        return "春"
    if month in (6, 7, 8):
        return "夏"
    if month in (9, 10, 11):
        return "秋"
    return "冬"


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
        # race_stageが未取得で、今回の画像から読み取れていれば補完する
        if existing.race_stage is None and parsed.get("race_stage"):
            existing.race_stage = parsed.get("race_stage")
            db.commit()
        if existing.weather is None and parsed.get("weather"):
            existing.weather = parsed.get("weather")
            db.commit()
        if existing.temperature_c is None and parsed.get("temperature_c") is not None:
            existing.temperature_c = parsed.get("temperature_c")
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
        race_stage=parsed.get("race_stage"),
        event_title=parsed.get("event_title"),
        source_app=parsed.get("source_app"),
        weather=parsed.get("weather"),
        temperature_c=parsed.get("temperature_c"),
        season=_current_season_jst(),
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
            "pre_race_comment": e.get("pre_race_comment"),
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


def _apply_final_odds(db: Session, race: models.Race, odds_list: list) -> int:
    """
    投票締切後に撮った「最終オッズ」スクショから、該当レースの購入履歴(pending/確定済み問わず)へ
    final_oddsを反映する。手入力の手間なく、オッズ変動の実績(odds_drift)を溜められるようにするため。
    """
    odds_by_key = {
        (o.get("bet_type"), o.get("combination")): o.get("odds_value")
        for o in odds_list
        if o.get("bet_type") and o.get("combination") and o.get("odds_value") is not None
    }
    if not odds_by_key:
        return 0
    purchases = db.query(models.Purchase).filter(models.Purchase.race_id == race.id).all()
    updated_count = 0
    for p in purchases:
        odds_value = odds_by_key.get((p.bet_type, p.combination))
        if odds_value is None:
            continue
        p.final_odds = odds_value
        updated_count += 1
    if updated_count:
        db.commit()
    return updated_count


@router.post("/screenshots")
async def analyze_screenshots(
    files: List[UploadFile] = File(...),
    is_final_odds: bool = Form(False),
    db: Session = Depends(get_db),
):
    """
    複数枚のスクショ(出走表・成績・勝率・オッズを問わず)をまとめてアップロードし、
    Geminiで解析してDBに反映する。

    Gemini解析(parse_screenshot)はネットワーク待ちが主体のため、従来は枚数分を
    1枚ずつ順番に呼び出しており、枚数に比例して処理時間が伸びていた(投票締切に
    間に合わないとの指摘あり)。ここを並列実行に変更し、時間を短縮する。

    is_final_odds=Trueの場合、投票締切後の最終オッズのスクショとして扱い、
    オッズ通常反映に加えて該当レースの購入履歴のfinal_oddsも自動で埋める
    (手入力なしでオッズ変動の実績データを溜められるようにするため)。
    """
    results = []
    affected_races = {}
    final_odds_updated_total = 0

    file_infos = []
    for f in files:
        content = await f.read()
        mime = f.content_type or "image/png"
        file_infos.append((f.filename, content, mime))

    # 複数枚のGemini解析を並列実行(1枚ずつの逐次待ちをなくす)。
    # ただし無料枠のレート制限(30回/分)に一斉に引っかかると、まとめて20秒待ちが
    # 発生し逆効果になるため、同時実行数に上限を設けて少しずつずらして送る。
    semaphore = asyncio.Semaphore(8)

    async def _parse_with_limit(content: bytes, mime: str):
        async with semaphore:
            return await asyncio.to_thread(parse_screenshot, content, mime)

    parsed_list = await asyncio.gather(
        *[_parse_with_limit(content, mime) for (_, content, mime) in file_infos],
        return_exceptions=True,
    )

    error_count = 0
    for (filename, _, _), parsed in zip(file_infos, parsed_list):
        if isinstance(parsed, Exception):
            error_count += 1
            results.append({"filename": filename, "error": str(parsed)})
            continue

        race = _get_or_create_race(db, parsed)
        affected_races[race.id] = race

        if parsed.get("entries"):
            _upsert_entries(db, race, parsed["entries"])
        if parsed.get("odds_list"):
            _upsert_odds(db, race, parsed["odds_list"])
            if is_final_odds:
                final_odds_updated_total += _apply_final_odds(db, race, parsed["odds_list"])
        if parsed.get("lines"):
            race.lines_data = parsed["lines"]
            db.commit()

        results.append({
            "filename": filename,
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
        # 注意: app_win_rate(アプリ側勝率)はここに意図的に含めない。
        # AI推定はアプリ勝率を見ずに独立した意見を出し、後段でtipstar実績とAI実績の
        # 信頼度(source_weights)に応じて重み付け合成する。ここに含めると合成時に
        # アプリ勝率の影響を二重に効かせてしまうため(のんとの設計相談により決定)。
        entries_payload = [
            {
                "car_number": e.car_number,
                "player_name": e.player_name,
                "region": e.region,
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
                "is_local": e.is_local,
                "pre_race_comment": e.pre_race_comment,
            }
            for e in entries
        ]
        weather_info = {
            "weather": race.weather,
            "temperature_c": race.temperature_c,
            "season": race.season,
        }
        try:
            bank = race.bank
            bank_info = None
            if bank:
                bank_info = {
                    "lap_length_m": bank.lap_length_m,
                    "home_stretch_length_m": bank.home_stretch_length_m,
                    "lead_advantage_score": bank.lead_advantage_score,
                }

            # 1段階目: 展開予想を先に生成する(選手構成が変わった可能性があるので毎回更新する)
            try:
                development = simulate_race_development(
                    entries_payload, lines=race.lines_data, bank_info=bank_info,
                    race_stage=race.race_stage, grade=race.grade,
                    weather_info=weather_info,
                )
                race.development_simulation = development
                db.commit()
            except Exception:
                development = race.development_simulation  # 失敗時は前回分があればそれを使う

            # 2段階目: 展開予想を踏まえて勝率を推定する
            ai_probs = estimate_ai_win_probabilities(
                entries_payload, lines=race.lines_data, bank_info=bank_info,
                race_stage=race.race_stage, grade=race.grade,
                development_simulation=development,
                weather_info=weather_info,
            )
        except Exception:
            ai_probs = {}

        # tipstar勝率とAI推定、どちらが実際に精度が高いかを実績から取得(データ不足時は1:1)
        try:
            weights = purchases_router.source_weights(db)
        except Exception:
            weights = {"app_weight": 0.5, "ai_weight": 0.5}

        for e in entries:
            ai_p = ai_probs.get(e.car_number)
            if ai_p is not None:
                e.ai_win_prob = ai_p
            app_p = (e.app_win_rate / 100.0) if e.app_win_rate is not None else None
            if ai_p is not None and app_p is not None:
                e.blended_win_prob = ai_p * weights["ai_weight"] + app_p * weights["app_weight"]
            elif ai_p is not None:
                e.blended_win_prob = ai_p
            elif app_p is not None:
                e.blended_win_prob = app_p
        db.commit()

    return {
        "processed_files": len(files),
        "error_count": error_count,
        "final_odds_updated_count": final_odds_updated_total,
        "results": results,
        "race_ids": list(affected_races.keys()),
    }
