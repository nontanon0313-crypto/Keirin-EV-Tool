from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timedelta
import time as _time

from ..database import get_db
from .. import models

router = APIRouter(prefix="/races", tags=["races"])


@router.post("/{race_id}/confirm-result")
def confirm_race_result(race_id: int, actual_result: str, db: Session = Depends(get_db)):
    """
    レースの実際の着順を1回入力するだけで、そのレースに紐づく全ての未確定購入を
    自動で的中/不的中判定する。
    actual_result: 例 "2-5-1" (1着2番、2着5番、3着1番)。
    同着がある場合は"="で区切って入力する(例: "7-14=9" → 1着7番、2着は14番と9番の同着)。

    証拠金残高はここでは変動させない。証拠金はユーザー自身の資金管理として独立させ、
    予想・投票プラン・集計・検証には影響させない方針のため(のんの要望により変更)。

    2026-09-04 再修正:
    bulk_update_mappings でも「行ごとのUPDATE」が発行され、さらに ORM オブジェクトへ
    属性代入したまま commit するとユニット・オブ・ワークが同じ更新を二重発行する。
    Neon(遠距離)では 1往復 ~200ms × 200件 ≒ 40秒になる。
    → ORMを汚さず、VALUES句1本のUPDATEでまとめて書く。
    """
    from .. import ev_calculator as calc

    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    try:
        parsed_result = calc.parse_actual_result(actual_result)
        if not parsed_result["groups"]:
            raise ValueError("empty")
    except ValueError:
        raise HTTPException(400, "着順の形式が正しくありません(例: 2-5-1、同着は7-14=9のように=で区切る)")

    race.actual_result = actual_result

    pending = (
        db.query(models.Purchase)
        .filter(models.Purchase.race_id == race_id, models.Purchase.result == "pending")
        .all()
    )

    odds_map = {}
    for o in db.query(models.Odds).filter(models.Odds.race_id == race_id).all():
        if o.odds_value and o.odds_value > 0:
            odds_map[(o.bet_type, o.combination)] = float(o.odds_value)

    updated = []
    purchase_rows = []  # (id, result, payout_amount, final_odds)
    for p in pending:
        final_odds = p.final_odds
        if final_odds is None:
            ov = odds_map.get((p.bet_type, p.combination))
            if ov:
                final_odds = ov
        is_win = calc.judge_purchase_result(p.bet_type, p.combination, parsed_result)
        settlement_odds = final_odds if final_odds is not None else p.odds_at_purchase
        payout = round(p.stake_amount * settlement_odds) if (is_win and settlement_odds) else 0
        result = "win" if is_win else "lose"
        # ORMには触らない（dirtyにすると commit 時に個別UPDATE が再発行される）
        purchase_rows.append((p.id, result, float(payout), final_odds))
        updated.append({
            "purchase_id": p.id,
            "bet_type": p.bet_type,
            "combination": p.combination,
            "result": result,
            "payout_amount": payout,
            "payout_is_estimated": final_odds is None,
        })

    skipped_list = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.race_id == race_id, models.SkippedBet.actual_result.is_(None))
        .all()
    )
    skipped_rows = []  # (id, actual_result, actual_payout)
    for s in skipped_list:
        is_win = calc.judge_purchase_result(s.bet_type, s.combination, parsed_result)
        if is_win:
            actual_result_value = "win"
            ov = odds_map.get((s.bet_type, s.combination))
            if ov:
                actual_payout_value = float(round(100.0 * ov))
            elif s.win_prob_estimated and s.win_prob_estimated > 0:
                implied_odds = 1.0 / s.win_prob_estimated
                actual_payout_value = float(round(100 * implied_odds))
            else:
                actual_payout_value = None
        else:
            actual_result_value = "lose"
            actual_payout_value = 0.0
        skipped_rows.append((s.id, actual_result_value, actual_payout_value))

    # --- 1本の SQL で一括更新（ネットワーク往復を最小化） ---
    if purchase_rows:
        # VALUES (id, result, payout, final_odds)
        values_sql = []
        params = {}
        for i, (pid, result, payout, final_odds) in enumerate(purchase_rows):
            values_sql.append(
                f"(CAST(:pid_{i} AS INTEGER), CAST(:pres_{i} AS TEXT), "
                f"CAST(:ppay_{i} AS DOUBLE PRECISION), CAST(:pfo_{i} AS DOUBLE PRECISION))"
            )
            params[f"pid_{i}"] = pid
            params[f"pres_{i}"] = result
            params[f"ppay_{i}"] = payout
            params[f"pfo_{i}"] = final_odds
        sql = f"""
            UPDATE purchases AS p SET
                result = v.result,
                payout_amount = v.payout_amount,
                final_odds = COALESCE(v.final_odds, p.final_odds)
            FROM (VALUES {', '.join(values_sql)})
                AS v(id, result, payout_amount, final_odds)
            WHERE p.id = v.id
        """
        db.execute(text(sql), params)

    if skipped_rows:
        values_sql = []
        params = {}
        for i, (sid, ares, apay) in enumerate(skipped_rows):
            values_sql.append(
                f"(CAST(:sid_{i} AS INTEGER), CAST(:sres_{i} AS TEXT), "
                f"CAST(:spay_{i} AS DOUBLE PRECISION))"
            )
            params[f"sid_{i}"] = sid
            params[f"sres_{i}"] = ares
            params[f"spay_{i}"] = apay
        sql = f"""
            UPDATE skipped_bets AS s SET
                actual_result = v.actual_result,
                actual_payout = v.actual_payout
            FROM (VALUES {', '.join(values_sql)})
                AS v(id, actual_result, actual_payout)
            WHERE s.id = v.id
        """
        db.execute(text(sql), params)

    db.commit()

    return {
        "race_id": race_id,
        "actual_result": actual_result,
        "updated_count": len(updated),
        "updated": updated,
        "skipped_updated_count": len(skipped_rows),
    }



@router.get("/")
def list_races(db: Session = Depends(get_db)):
    """投票タブ用: 当日(JST)かつ未確定のみ。前日の未確定が残らないようにする。"""
    now = _jst_now_naive()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.is_(None))
        .filter(models.Race.race_date >= today_start, models.Race.race_date < today_end)
        .order_by(models.Race.id.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "grade": r.grade,
            "event_title": r.event_title,
            "race_date": r.race_date.strftime("%m/%d") if r.race_date else None,
            "entry_count": len(r.entries),
            "odds_count": len(r.odds_list),
        }
        for r in races
        if len(r.entries) > 0
    ]



@router.delete("/")
def delete_all_races(db: Session = Depends(get_db)):
    """過去分を含む全レース関連データを削除(証拠金は対象外)。"""
    n_purchases = db.query(models.Purchase).delete()
    n_skipped = db.query(models.SkippedBet).delete()
    n_ev = db.query(models.EvResult).delete()
    n_odds = db.query(models.Odds).delete()
    n_entries = db.query(models.Entry).delete()
    n_races = db.query(models.Race).delete()
    db.commit()
    return {
        "deleted_all": True,
        "races": n_races,
        "entries": n_entries,
        "odds": n_odds,
        "purchases": n_purchases,
        "skipped_bets": n_skipped,
        "ev_results": n_ev,
    }


@router.post("/{race_id}/reset-for-reanalysis")
def reset_race_for_reanalysis(race_id: int, db: Session = Depends(get_db)):
    """
    出走表・オッズ・レース結果はそのまま残し、予想に関わるデータ(AI勝率・購入記録・
    見送り記録・EV計算結果)だけを削除して、そのレースを「取り込み直後」の状態に戻す。

    レース取得(スクレイピング)は1日分で数時間かかるため、予想ロジック(AIプロンプト等)を
    変更するたびに新規データを取り直すのは非現実的。既存の出走表・オッズ・結果を
    再利用したまま「予想→投票→結果検証→予想精度確認」のループを再実行できるように
    するために追加した(のんの要望により追加)。

    証拠金残高はここでは一切触らない。証拠金はユーザー自身の資金管理として独立させ、
    購入記録の削除・再作成に連動させない方針のため(のんの要望により変更)。
    """
    race = db.query(models.Race).filter(models.Race.id == race_id).first()
    if not race:
        raise HTTPException(status_code=404, detail="レースが見つかりません")

    purchases_deleted = db.query(models.Purchase).filter(models.Purchase.race_id == race_id).count()
    skipped_deleted = db.query(models.SkippedBet).filter(models.SkippedBet.race_id == race_id).delete()
    ev_results_deleted = db.query(models.EvResult).filter(models.EvResult.race_id == race_id).delete()
    db.query(models.Purchase).filter(models.Purchase.race_id == race_id).delete()

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    for e in entries:
        e.ai_win_prob = None
        e.blended_win_prob = None

    db.commit()

    return {
        "race_id": race_id,
        "purchases_deleted": purchases_deleted,
        "skipped_bets_deleted": skipped_deleted,
        "ev_results_deleted": ev_results_deleted,
        "entries_reset": len(entries),
        "message": "出走表・オッズ・結果は保持したまま、予想関連データをリセットしました。/analyze/estimateから再実行できます",
    }


@router.post("/reset-for-reanalysis/batch")
def reset_races_for_reanalysis_batch(payload: dict, db: Session = Depends(get_db)):
    """複数レースをまとめてリセットする。payload={"race_ids":[..]}。1件の失敗が他を止めないようにする。"""
    race_ids = payload.get("race_ids", [])
    results = []
    for rid in race_ids:
        try:
            results.append(reset_race_for_reanalysis(rid, db))
        except HTTPException as e:
            results.append({"race_id": rid, "error": e.detail})
        except Exception as e:
            results.append({"race_id": rid, "error": str(e)})
    return {
        "total": len(race_ids),
        "succeeded": sum(1 for r in results if "error" not in r),
        "failed": sum(1 for r in results if "error" in r),
        "results": results,
    }



@router.post("/repair-broken-results")
def repair_broken_results(db: Session = Depends(get_db), apply: bool = False):
    """
    actual_result が車番形式(例: 1-2-3)でないレースを洗い出す。
    apply=true のときは actual_result をクリアし、紐づく購入を pending に戻す
    (正しい結果で再confirmできるようにする)。
    DB切替後や結果パーサ修正後の復旧用。
    """
    import re
    races = db.query(models.Race).filter(models.Race.actual_result.isnot(None)).all()
    broken = []
    ok_re = re.compile(r"^[1-9]([-=][1-9]){1,2}$")
    for race in races:
        ar = (race.actual_result or "").strip()
        if ok_re.match(ar):
            continue
        item = {
            "race_id": race.id,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
            "race_date": race.race_date.strftime("%Y-%m-%d") if race.race_date else None,
            "external_ref": race.external_ref,
            "actual_result": ar,
        }
        if apply:
            race.actual_result = None
            pending_reset = 0
            for p in db.query(models.Purchase).filter(models.Purchase.race_id == race.id).all():
                if p.result in ("win", "lose"):
                    p.result = "pending"
                    p.payout_amount = 0
                    pending_reset += 1
            for s in db.query(models.SkippedBet).filter(models.SkippedBet.race_id == race.id).all():
                if s.actual_result is not None:
                    s.actual_result = None
                    s.actual_payout = None
            item["purchases_reset_to_pending"] = pending_reset
        broken.append(item)
    if apply and broken:
        db.commit()
    return {
        "broken_count": len(broken),
        "applied": apply,
        "items": broken,
        "note": "apply=falseは一覧のみ。apply=trueで結果を消し購入をpendingに戻す。その後JSONを再取得してconfirm-resultし直す",
    }


@router.get("/for-reanalysis")
def list_races_for_reanalysis(db: Session = Depends(get_db), limit: int = 100):
    """
    再予想(reset-for-reanalysis)の対象になりうるレース一覧。
    結果確定済み・出走表のみのレースも含め、出走表があるレース全てを対象にする
    (画面から手でコマンドを打たずに再予想できるようにするため、のんの要望により追加)。
    """
    races = (
        db.query(models.Race)
        .order_by(models.Race.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "race_date": r.race_date.strftime("%m/%d") if r.race_date else None,
            "entry_count": len(r.entries),
            "odds_count": len(r.odds_list),
            "actual_result": r.actual_result,
        }
        for r in races
        if len(r.entries) > 0
    ]


def _jst_now_naive():
    """
    JSTの「壁時計時刻」をタイムゾーン情報無しのdatetimeで返す。
    post_timeもJSTのタイムゾーン情報無しで保存しているため、比較の基準を揃える
    (のんの要望=当日・直前レース抽出機能により追加)。
    """
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)


@router.get("/today")
def list_races_today(db: Session = Depends(get_db)):
    """
    本日(JST)のレース一覧。発走時刻順に並べる。
    予想済みか(AI勝率が入っているか)も含める(のんの要望により追加)。
    """
    now = _jst_now_naive()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    races = (
        db.query(models.Race)
        .filter(models.Race.race_date >= today_start, models.Race.race_date < today_end)
        .filter(models.Race.actual_result.is_(None))
        .all()
    )
    result = []
    for r in races:
        entries = r.entries
        predicted = any(e.blended_win_prob is not None for e in entries)
        result.append({
            "race_id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "event_title": r.event_title,
            "post_time": r.post_time.strftime("%H:%M") if r.post_time else None,
            "riders_count": len(entries),
            "predicted": predicted,
            "actual_result": r.actual_result,
        })
    result.sort(key=lambda x: (x["post_time"] is None, x["post_time"] or ""))
    return result


@router.get("/upcoming")
def list_races_upcoming(within_min: int = 30, db: Session = Depends(get_db)):
    """発走予定時刻まで指定分数(既定30分)以内のレース一覧(のんの要望により追加)。"""
    now = _jst_now_naive()
    until = now + timedelta(minutes=within_min)
    races = (
        db.query(models.Race)
        .filter(models.Race.post_time.isnot(None))
        .filter(models.Race.post_time >= now, models.Race.post_time <= until)
        .filter(models.Race.actual_result.is_(None))
        .order_by(models.Race.post_time.asc())
        .all()
    )
    result = []
    for r in races:
        entries = r.entries
        predicted = any(e.blended_win_prob is not None for e in entries)
        mins_to_post = int((r.post_time - now).total_seconds() // 60)
        result.append({
            "race_id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "post_time": r.post_time.strftime("%H:%M"),
            "mins_to_post": mins_to_post,
            "riders_count": len(entries),
            "predicted": predicted,
        })
    return result


@router.get("/favorites")
def list_race_favorites(min_win_prob: float = 0.25, db: Session = Depends(get_db)):
    """本日(JST)かつ未確定の予想済みレースから本命候補を勝率降順で返す。"""
    now = _jst_now_naive()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    entries = (
        db.query(models.Entry)
        .filter(models.Entry.blended_win_prob.isnot(None))
        .filter(models.Entry.blended_win_prob >= min_win_prob)
        .all()
    )
    race_ids = {e.race_id for e in entries}
    races_by_id = {
        r.id: r
        for r in db.query(models.Race).filter(
            models.Race.id.in_(race_ids),
            models.Race.actual_result.is_(None),
            models.Race.race_date >= today_start,
            models.Race.race_date < today_end,
        ).all()
    } if race_ids else {}

    result = []
    for e in entries:
        race = races_by_id.get(e.race_id)
        if race is None:
            continue  # 結果確定済み、または存在しないレースは除外
        result.append({
            "race_id": race.id,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
            "post_time": race.post_time.strftime("%H:%M") if race.post_time else None,
            "car_number": e.car_number,
            "player_name": e.player_name,
            "win_prob_pct": round(e.blended_win_prob * 100, 1),
        })
    result.sort(key=lambda x: -x["win_prob_pct"])
    return result

@router.get("/skipped-bet-counts")
def skipped_bet_counts(db: Session = Depends(get_db)):
    """見送りテーブルの件数確認用。"""
    total = db.query(models.SkippedBet).count()
    with_result = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.in_(("win", "lose")))
        .count()
    )
    without = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.is_(None))
        .count()
    )
    by_reason = {}
    for s in db.query(models.SkippedBet.reason).limit(5000).all():
        r = (s[0] or "なし")[:40]
        by_reason[r] = by_reason.get(r, 0) + 1
    top_reasons = sorted(by_reason.items(), key=lambda x: -x[1])[:15]
    return {
        "total": total,
        "with_result": with_result,
        "without_result": without,
        "top_reasons": top_reasons,
    }


@router.post("/backfill-skip-results")
def backfill_skip_results(payload: dict = None, db: Session = Depends(get_db)):
    """
    確定済みレースの SkippedBet で actual_result が空のものを一括で埋める。
    payload: {"race_ids": [...]} 省略時は全確定レース。
    """
    from .. import ev_calculator as calc

    payload = payload or {}
    race_ids = payload.get("race_ids")
    if race_ids:
        races = db.query(models.Race).filter(models.Race.id.in_(race_ids)).all()
    else:
        races = db.query(models.Race).filter(models.Race.actual_result.isnot(None)).all()

    total_updated = 0
    races_touched = 0
    errors = []
    for race in races:
        if not race.actual_result:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception as e:
            errors.append({"race_id": race.id, "error": f"parse: {e}"})
            continue

        odds_map = {
            (o.bet_type, o.combination): float(o.odds_value)
            for o in db.query(models.Odds).filter(models.Odds.race_id == race.id).all()
            if o.odds_value and o.odds_value > 0
        }
        skips = (
            db.query(models.SkippedBet)
            .filter(
                models.SkippedBet.race_id == race.id,
                models.SkippedBet.actual_result.is_(None),
            )
            .all()
        )
        if not skips:
            continue
        n = 0
        for s in skips:
            is_win = calc.judge_purchase_result(s.bet_type, s.combination, parsed)
            s.actual_result = "win" if is_win else "lose"
            if is_win:
                ov = odds_map.get((s.bet_type, s.combination))
                if ov:
                    s.actual_payout = round(100.0 * ov)
                elif s.win_prob_estimated and s.ev_pct_estimated is not None and s.win_prob_estimated > 0:
                    implied = (1 + s.ev_pct_estimated / 100) / s.win_prob_estimated
                    s.actual_payout = round(100 * implied)
                else:
                    s.actual_payout = None
            else:
                s.actual_payout = 0.0
            n += 1
        total_updated += n
        races_touched += 1
        db.commit()

    return {
        "races_touched": races_touched,
        "skipped_bets_updated": total_updated,
        "errors": errors[:20],
    }


@router.post("/backfill-final-odds")
def backfill_final_odds(payload: dict = None, db: Session = Depends(get_db)):
    """
    Purchase.final_odds が空の行を、同一レースの Odds 表から埋める。
    過去データで最終オッズスクショが無い場合の代理。
    """
    payload = payload or {}
    race_ids = payload.get("race_ids")
    q = db.query(models.Purchase).filter(models.Purchase.final_odds.is_(None))
    if race_ids:
        q = q.filter(models.Purchase.race_id.in_(race_ids))
    purchases = q.all()
    if not purchases:
        return {"updated": 0, "examined": 0}

    by_race = {}
    for p in purchases:
        by_race.setdefault(p.race_id, []).append(p)

    updated = 0
    for rid, plist in by_race.items():
        odds_map = {
            (o.bet_type, o.combination): float(o.odds_value)
            for o in db.query(models.Odds).filter(models.Odds.race_id == rid).all()
            if o.odds_value and o.odds_value > 0
        }
        for p in plist:
            ov = odds_map.get((p.bet_type, p.combination))
            if ov:
                p.final_odds = ov
                updated += 1
        db.commit()

    return {"examined": len(purchases), "updated": updated}


@router.post("/{race_id}/soft-reset-bets")
def soft_reset_bets(race_id: int, db: Session = Depends(get_db)):
    """
    購入・見送り記録だけ消す。出走表・オッズ・結果・AI勝率は残す。
    「結果を忘れて再投票」の高速ルート用。
    """
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")
    n_p = db.query(models.Purchase).filter(models.Purchase.race_id == race_id).delete()
    n_s = db.query(models.SkippedBet).filter(models.SkippedBet.race_id == race_id).delete()
    db.commit()
    return {
        "race_id": race_id,
        "purchases_deleted": n_p,
        "skipped_deleted": n_s,
        "actual_result_kept": race.actual_result,
    }


@router.post("/{race_id}/replay-settled")
def replay_settled_one(
    race_id: int,
    bankroll: float = 1_000_000,
    apply_performance_gates: bool = True,
    apply_odds_safety_margin: bool = False,
    db: Session = Depends(get_db),
):
    """
    1レース: 購入/見送りを消す → 現行race-planで再投票 → 保存済み着順で再確定。
    Gemini再実行はしない(既存の選手勝率を使う)。高速にサンプルを作り直す用。
    """
    from .. import schemas
    from . import ev as ev_router

    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")
    if not race.actual_result:
        raise HTTPException(400, "actual_resultが無いレースはreplayできない")

    odds_n = db.query(models.Odds).filter(models.Odds.race_id == race_id).count()
    if odds_n <= 0:
        return {"race_id": race_id, "stage": "skipped_no_odds"}

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()

    def _has_prob(e):
        for attr in ("blended_win_prob", "ai_win_prob", "tipstar_win_prob"):
            if getattr(e, attr, None) is not None:
                return True
        return False

    if not entries or not any(_has_prob(e) for e in entries):
        return {
            "race_id": race_id,
            "stage": "skipped_no_win_probs",
            "message": "選手勝率が無い。先に /analyze/estimate か full reset+再予想が必要",
        }

    _t_reset_start = _time.time()
    soft_reset_bets(race_id, db)
    _t_reset_end = _time.time()

    req = schemas.RacePlanRequest(
        race_id=race_id,
        bankroll=bankroll,
        apply_performance_gates=apply_performance_gates,
        apply_odds_safety_margin=apply_odds_safety_margin,
        avoid_garami=False,  # replayは速度優先（ガミり厳密は任意）
    )
    _t_plan_start = _time.time()
    try:
        plan = ev_router.race_plan(race_id, req, db)
    except HTTPException as he:
        return {
            "race_id": race_id,
            "stage": "race_plan_http_error",
            "status_code": he.status_code,
            "detail": he.detail,
        }
    except Exception as e:
        return {
            "race_id": race_id,
            "stage": "race_plan_error",
            "error": f"{type(e).__name__}: {e}",
        }
    if plan.get("skipped_no_odds"):
        return {"race_id": race_id, "stage": "skipped_no_odds"}

    _t_plan_end = _time.time()

    items = plan.get("items") or []
    skipped_saved = plan.get("skipped_saved_count", 0)
    recorded = 0
    if items:
        objs = []
        for it in items:
            win_prob = it.get("win_prob")
            if win_prob is None and it.get("estimated_win_prob_pct") is not None:
                win_prob = float(it["estimated_win_prob_pct"]) / 100.0
            objs.append(
                models.Purchase(
                    race_id=race_id,
                    bet_type=it["bet_type"],
                    combination=it["combination"],
                    stake_amount=float(it.get("stake") or it.get("stake_amount") or 100),
                    odds_at_purchase=it.get("odds_value"),
                    win_prob_at_purchase=win_prob,
                    win_prob_raw=it.get("win_prob_raw"),
                    ev_pct_at_purchase=it.get("ev_pct"),
                    result="pending",
                    payout_amount=0,
                )
            )
        db.add_all(objs)
        db.commit()
        recorded = len(objs)

    _t_purchase_save_end = _time.time()

    conf = confirm_race_result(race_id, race.actual_result, db)

    _t_confirm_end = _time.time()

    # 実際の的中買い目がReplay後にどこへ入ったかを確認する。
    # 「候補生成漏れ」と「候補には存在したがフィルターで見送り」を区別するための
    # 小規模バックテスト用診断。DBスキーマは変更しない。
    from .. import ev_calculator as calc

    winning_diagnostics = []
    try:
        actual = calc.parse_actual_result(race.actual_result)
        target_bet_types = {"2車単", "2車複", "3連単", "3連複", "ワイド"}

        purchases_by_key = {
            (p.bet_type, p.combination): p
            for p in db.query(models.Purchase)
            .filter(models.Purchase.race_id == race_id)
            .all()
        }
        skipped_by_key = {
            (s.bet_type, s.combination): s
            for s in db.query(models.SkippedBet)
            .filter(models.SkippedBet.race_id == race_id)
            .all()
        }

        for o in db.query(models.Odds).filter(models.Odds.race_id == race_id).all():
            if o.bet_type not in target_bet_types:
                continue
            if not calc.judge_purchase_result(o.bet_type, o.combination, actual):
                continue

            key = (o.bet_type, o.combination)
            purchase = purchases_by_key.get(key)
            skipped = skipped_by_key.get(key)

            if purchase is not None:
                status = "purchase"
                reason = None
                prob = purchase.win_prob_at_purchase
                ev = purchase.ev_pct_at_purchase
            elif skipped is not None:
                status = "skipped"
                reason = skipped.reason
                prob = skipped.win_prob_estimated
                ev = skipped.ev_pct_estimated
            else:
                status = "not_recorded"
                reason = "Purchase/SkippedBetのどちらにも記録されていない"
                prob = None
                ev = None

            winning_diagnostics.append({
                "bet_type": o.bet_type,
                "combination": o.combination,
                "odds": o.odds_value,
                "status": status,
                "reason": reason,
                "win_prob_pct": round(prob * 100, 3) if prob is not None else None,
                "ev_pct": round(ev, 2) if ev is not None else None,
            })
    except Exception as e:
        winning_diagnostics = [{
            "status": "diagnostic_error",
            "reason": str(e),
        }]

    from . import purchases as purchases_router_for_debug

    return {
        "race_id": race_id,
        "stage": "done",
        "plan_items": len(items),
        "purchases_recorded": recorded,
        "skipped_saved_count": skipped_saved,
        "skipped_candidate_count": plan.get("skipped_candidate_count"),
        "winning_diagnostics": winning_diagnostics,
        "confirm": {
            "updated_count": conf.get("updated_count"),
            "skipped_updated_count": conf.get("skipped_updated_count"),
            "actual_result": conf.get("actual_result"),
        },
        # キャッシュが効いていない(毎回47秒前後)報告の原因調査用。
        # process_pidがレースごとに変わっていれば、Render側でプロセスが
        # 再起動されている(=メモリ制限等)ことが確定する。
        "debug_process_pid": purchases_router_for_debug._PROCESS_PID,
        "debug_process_uptime_seconds": round(
            _time.time() - purchases_router_for_debug._PROCESS_STARTED_AT, 1
        ),
        # 1レースの内訳(秒)。race_plan内部のdebug_timingsと合わせて見ることで、
        # 「校正/ゲート集計(キャッシュ済み)」「候補ループ」「見送り保存」
        # 「購入保存」「確定処理」のどこで時間を使っているかを特定する。
        "debug_outer_timings": {
            "soft_reset_bets": round(_t_reset_end - _t_reset_start, 2),
            "race_plan_call_total": round(_t_plan_end - _t_plan_start, 2),
            "purchase_save": round(_t_purchase_save_end - _t_plan_end, 2),
            "confirm_race_result": round(_t_confirm_end - _t_purchase_save_end, 2),
        },
        "debug_race_plan_timings": plan.get("debug_timings"),
    }



@router.get("/replay-settled/targets")
def replay_settled_targets(
    since: str = None,
    limit: int = 5000,
    after_race_id: int = None,
    db: Session = Depends(get_db),
):
    """再投票対象のrace_id一覧（進捗付きクライアント用）。"""
    from . import purchases as purchases_router
    from sqlalchemy import or_

    if since in (None, "", "all", "*"):
        since_dt = None
        since_out = "all"
    else:
        since_dt = purchases_router._parse_since_param(since)
        since_out = since

    # Race + Entry をJOINして、勝率のあるsettled raceだけをDB側で抽出。
    # 旧実装のraceごとのEntry問い合わせ（N+1）を解消する。
    q = (
        db.query(models.Race.id)
        .join(models.Entry, models.Entry.race_id == models.Race.id)
        .filter(models.Race.actual_result.isnot(None))
        .filter(
            or_(
                models.Entry.blended_win_prob.isnot(None),
                models.Entry.ai_win_prob.isnot(None),
                models.Entry.tipstar_win_prob.isnot(None),
            )
        )
        .distinct()
        .order_by(models.Race.id.asc())
    )

    if since_dt is not None:
        q = q.filter(
            or_(
                models.Race.race_date >= since_dt,
                models.Race.race_date.is_(None),
            )
        )

    if after_race_id is not None:
        q = q.filter(models.Race.id > after_race_id)

    ids = [row[0] for row in q.limit(limit).all()]

    return {
        "since": since_out,
        "after_race_id": after_race_id,
        "total": len(ids),
        "race_ids": ids,
    }


@router.post("/replay-settled/batch")
def replay_settled_batch(payload: dict, db: Session = Depends(get_db)):
    """
    確定済みレースをまとめて「旧投票記録を完全置換」して再投票→再結果検証。

    重要:
    - 対象レースの Purchase / SkippedBet は再投票開始前に一括削除する。
    - Race / Entry / Odds / actual_result / AI勝率は削除しない。
    - Gemini等のAI再実行はしない。既存の勝率を使用する。
    - race_ids指定時はsinceより優先する。
    - 再投票に失敗したレースについても旧Purchase/SkippedBetは復元しない。
      「再賭け対象期間のデータを現行ロジックへ置換する」ことを保証する。
    """
    from . import purchases as purchases_router
    from sqlalchemy import func

    payload = payload or {}
    bankroll = float(payload.get("bankroll") or 1_000_000)
    limit = int(payload.get("limit") or 5000)
    race_ids = payload.get("race_ids")
    since_raw = payload.get("since")

    # None / 空 / all / * は全確定レース
    if since_raw in (None, "", "all", "*"):
        since = None
        since_dt = None
    else:
        since = since_raw
        since_dt = purchases_router._parse_since_param(since)

    # 対象race_idを確定
    if race_ids:
        ids = list(dict.fromkeys(int(rid) for rid in race_ids))[:limit]
    else:
        q = (
            db.query(models.Race.id)
            .filter(models.Race.actual_result.isnot(None))
            .order_by(models.Race.id.asc())
        )
        if since_dt is not None:
            q = q.filter(models.Race.race_date >= since_dt)
        ids = [r[0] for r in q.limit(limit).all()]

    if not ids:
        return {
            "replace_mode": True,
            "total": 0,
            "old_purchases_deleted": 0,
            "old_skipped_bets_deleted": 0,
            "done": 0,
            "failed_or_skipped": 0,
            "since": since,
            "results": [],
        }

    # 実在する対象レースだけを置換対象にする
    existing_ids = [
        r[0]
        for r in (
            db.query(models.Race.id)
            .filter(models.Race.id.in_(ids))
            .all()
        )
    ]

    # 再投票前に対象期間の旧Purchase/SkippedBetを一括削除。
    # Race/Entry/Odds/actual_result/AI勝率は一切触らない。
    old_purchases_deleted = (
        db.query(models.Purchase)
        .filter(models.Purchase.race_id.in_(existing_ids))
        .delete(synchronize_session=False)
        if existing_ids else 0
    )
    old_skipped_deleted = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.race_id.in_(existing_ids))
        .delete(synchronize_session=False)
        if existing_ids else 0
    )
    db.commit()

    results = []

    for rid in ids:
        try:
            results.append(
                replay_settled_one(
                    rid,
                    bankroll=bankroll,
                    db=db,
                )
            )
        except HTTPException as e:
            results.append({
                "race_id": rid,
                "stage": "error",
                "error": e.detail,
            })
        except Exception as e:
            # 1レースの失敗で他レースの再投票を止めない
            db.rollback()
            results.append({
                "race_id": rid,
                "stage": "error",
                "error": str(e),
            })

    done = sum(1 for r in results if r.get("stage") == "done")
    failed_or_skipped = len(ids) - done

    return {
        "replace_mode": True,
        "total": len(ids),
        "existing_races": len(existing_ids),
        "old_purchases_deleted": old_purchases_deleted,
        "old_skipped_bets_deleted": old_skipped_deleted,
        "done": done,
        "failed_or_skipped": failed_or_skipped,
        "since": since,
        "results": results,
    }



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
        "actual_result": race.actual_result,
        "venue_name": race.venue_name,
        "race_number": race.race_number,
        "race_date": race.race_date.strftime("%Y-%m-%d") if race.race_date else None,
        "external_ref": race.external_ref,
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
                # OCRでは「欠場」を直接判定できないため、アプリ勝率がちょうど0%かつ
                # 他の基本データ(競走得点)も無い場合のみ、目視確認を促す。
                # race_scoreなど他のデータがあれば実際に出走している証拠なので、
                # 単に勝率が低いだけと判断できる(のんの指摘により条件を絞った)。
                "zero_app_win_rate_warning": e.app_win_rate == 0 and e.race_score is None,
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

