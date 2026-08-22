"""
oddspark.comスクレイパー(scraper/keirin_oddspark_scraper.py)が出力したJSONを
Race/Entry/Oddsテーブルに取り込むためのルーター。

のんの要望「予想→結果記録→検証の流れができるようにデータ登録してほしい」を受けて追加。

設計方針:
- 同一レースの再取り込みはexternal_ref(例:"oddspark:44:20260818:12")で冪等にする
  (同じレースを二重登録しない)
- 出走表(entry)・結果(result)は取れている分をそのまま取り込む
  (賭け金に直結しないため、多少の欠けがあっても価値がある)
- オッズ(odds)は券種ごとにis_complete=Trueのものだけ取り込む
  (is_complete=通信エラーなく取得できたかを表す。理論上の組み合わせ数との
  一致は求めない。人気のない組み合わせに1票も入らずオッズが存在しない
  ことは正常であり、欠損ではないと判明したため)
  (欠損・誤った組み合わせのオッズがEV計算に混入するとベット判断を誤らせるため、
  不完全なデータは安全側に倒して取り込まない)
- jo_code(場コード)がJO_CODE_VERIFIED_BLOCKSに無い場合は取り込みを拒否せず、
  レスポンスに警告を含めて手動確認を促す(未検証コードを黙って信用しない)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from ..database import get_db
from .. import models
from ..keirin_data import venue_name_from_jo_code

router = APIRouter(prefix="/scraper-import", tags=["scraper-import"])

# oddspark表記→アプリ内表記の脚質マッピング(不明な値はそのまま素通しする)
LEG_STYLE_MAP = {"逃": "逃げ", "追": "追込", "両": "両方"}


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_odds_float(v):
    """
    オッズ値を数値に変換する。ワイドは「8.7-10.1」のような範囲表記のことがあり、
    その場合は下限値を使う(実際の払い戻しがどちらに転ぶか分からないため、
    期待値を過大評価しない安全側の選択。のんの実機検証を受けて追加)。
    """
    if v is None:
        return None
    s = str(v)
    if "-" in s:
        s = s.split("-")[0]
    return _to_float(s)


@router.post("/race")
def import_scraped_race(payload: dict, db: Session = Depends(get_db)):
    jo_code = str(payload.get("jo_code", ""))
    kaisai_bi = str(payload.get("kaisai_bi", ""))
    race_no = payload.get("race_no")
    if not (jo_code and kaisai_bi and race_no):
        raise HTTPException(status_code=400, detail="jo_code/kaisai_bi/race_noが不足しています")

    warnings = []
    venue_info = venue_name_from_jo_code(jo_code)
    if venue_info is None:
        raise HTTPException(status_code=400, detail=f"未知のjo_code({jo_code})です。app/keirin_data.pyのJO_CODE_TO_VENUEに追加してください")
    venue_name, verified = venue_info
    if not verified:
        warnings.append(f"jo_code={jo_code}({venue_name})は場コード対応が未検証(推定)です。念のため会場名をご確認ください")

    external_ref = f"oddspark:{jo_code}:{kaisai_bi}:{race_no}"
    race = db.query(models.Race).filter(models.Race.external_ref == external_ref).first()

    entry = payload.get("entry") or {}
    riders = entry.get("riders") or []
    race_date = None
    try:
        race_date = datetime.strptime(kaisai_bi, "%Y%m%d")
    except ValueError:
        pass

    if race is None:
        race = models.Race(
            venue_name=venue_name,
            race_number=int(race_no),
            event_title=entry.get("race_name") or None,
            race_date=race_date,
            source_app="oddspark_scraper",
            external_ref=external_ref,
        )
        db.add(race)
        db.flush()
    else:
        # 再取り込み時は出走表由来の情報だけ更新する(手動で編集した他の項目は上書きしない)
        if entry.get("race_name"):
            race.event_title = entry["race_name"]

    # --- 出走表(entry)の取り込み ---
    entries_created = 0
    entries_updated = 0
    if riders:
        existing_entries = {e.car_number: e for e in db.query(models.Entry).filter(models.Entry.race_id == race.id).all()}
        for r in riders:
            car_no = _to_int(r.get("車番"))
            if car_no is None or not r.get("選手名"):
                continue
            leg_style_raw = r.get("脚質") or ""
            leg_style = LEG_STYLE_MAP.get(leg_style_raw, leg_style_raw or None)
            existing = existing_entries.get(car_no)
            if existing:
                existing.player_name = r.get("選手名") or existing.player_name
                existing.region = r.get("地区") or existing.region
                existing.player_class = r.get("級班") or existing.player_class
                existing.age = _to_int(r.get("年齢")) or existing.age
                existing.period = r.get("期") or existing.period
                existing.race_score = _to_float(r.get("競走得点")) or existing.race_score
                existing.leg_style = leg_style or existing.leg_style
                entries_updated += 1
            else:
                db.add(models.Entry(
                    race_id=race.id,
                    car_number=car_no,
                    player_name=r.get("選手名"),
                    region=r.get("地区") or None,
                    player_class=r.get("級班") or None,
                    age=_to_int(r.get("年齢")),
                    period=r.get("期") or None,
                    race_score=_to_float(r.get("競走得点")),
                    leg_style=leg_style,
                ))
                entries_created += 1
    else:
        warnings.append("出走表データが空でした(スクレイパー側でentry_errorが出ていないか確認してください)")

    # --- オッズ(odds)の取り込み: 通信エラーなく取得できた券種のみ ---
    # (件数が理論値より少ないのは、票が入らなかった組み合わせがあるだけで正常)
    odds_created = 0
    odds_skipped_bet_types = []
    odds_payload = payload.get("odds") or {}
    if odds_payload:
        # 同一レースの既存オッズは一旦洗い替える(再取り込み時に古い不完全データを残さないため)
        db.query(models.Odds).filter(models.Odds.race_id == race.id).delete()
        for bet_type, info in odds_payload.items():
            if not info.get("is_complete"):
                odds_skipped_bet_types.append({
                    "bet_type": bet_type,
                    "got": info.get("matrix_count"),
                    "expected": info.get("expected_count"),
                })
                continue
            for item in info.get("matrix", []):
                odds_v = _to_odds_float(item.get("オッズ"))
                if odds_v is None:
                    continue
                if bet_type == "3連単" or bet_type == "3連複":
                    combo = f"{item['1着']}-{item['2着']}-{item['3着']}"
                else:
                    combo = f"{item['1着']}-{item['2着']}"
                db.add(models.Odds(
                    race_id=race.id,
                    bet_type=bet_type,
                    combination=combo,
                    odds_value=odds_v,
                ))
                odds_created += 1
        if odds_skipped_bet_types:
            warnings.append(f"以下の券種は通信エラーのため取り込みませんでした: {odds_skipped_bet_types}")

    # --- 結果(result)の取り込み ---
    result_payload = payload.get("result") or {}
    race_results = result_payload.get("results") or []
    if race_results:
        by_place = {r["着順"]: r["車番"] for r in race_results}
        try:
            parts = [by_place[str(p)] for p in (1, 2, 3) if str(p) in by_place]
            if len(parts) == 3:
                race.actual_result = "-".join(parts)
        except Exception:
            warnings.append("結果の着順データの形式が想定外でした。手動でご確認ください")
    else:
        warnings.append("結果データが空でした(レース未確定、またはresult_errorが出ていないか確認してください)")

    db.commit()
    db.refresh(race)

    return {
        "race_id": race.id,
        "external_ref": external_ref,
        "venue_name": venue_name,
        "venue_code_verified": verified,
        "entries_created": entries_created,
        "entries_updated": entries_updated,
        "odds_created": odds_created,
        "odds_skipped_bet_types": odds_skipped_bet_types,
        "actual_result_recorded": race.actual_result,
        "warnings": warnings,
    }


@router.post("/batch")
def import_scraped_batch(payload: dict, db: Session = Depends(get_db)):
    """
    複数レース分をまとめて取り込む。payload = {"races": [race_json, race_json, ...]}
    1レースの取り込み失敗が他のレースを止めないよう、レースごとにtry/exceptする。
    """
    races_payload = payload.get("races") or []
    results = []
    for race_json in races_payload:
        try:
            result = import_scraped_race(race_json, db)
            results.append({"ok": True, **result})
        except HTTPException as e:
            results.append({"ok": False, "error": e.detail, "jo_code": race_json.get("jo_code"), "kaisai_bi": race_json.get("kaisai_bi"), "race_no": race_json.get("race_no")})
        except Exception as e:
            results.append({"ok": False, "error": str(e), "jo_code": race_json.get("jo_code"), "kaisai_bi": race_json.get("kaisai_bi"), "race_no": race_json.get("race_no")})
    return {
        "total": len(races_payload),
        "succeeded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
