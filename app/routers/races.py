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
    actual_result: 例 "2-5-1" (1着2番、2着5番、3着1番)。
    同着がある場合は"="で区切って入力する(例: "7-14=9" → 1着7番、2着は14番と9番の同着)。
    """
    from .. import ev_calculator as calc
    from . import bankroll as bankroll_router

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
        is_win = calc.judge_purchase_result(p.bet_type, p.combination, parsed_result)
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

    # 見送り記録(SkippedBet)にも同じ着順を適用し、「除外して正解だったか」を検証できるようにする
    # (のんの指摘により追加。以前はレース確定してもSkippedBetの結果が一切埋まらなかった)。
    skipped = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.race_id == race_id, models.SkippedBet.actual_result.is_(None))
        .all()
    )
    skipped_updated = 0
    for s in skipped:
        is_win = calc.judge_purchase_result(s.bet_type, s.combination, parsed_result)
        s.actual_result = "win" if is_win else "lose"
        # 見送り分は実際には購入していないため、購入時オッズが無い。想定EVから逆算した
        # オッズ相当(1+EV%/100)/推定確率、が無理なら払戻推定はNoneのままにする。
        if is_win and s.win_prob_estimated:
            implied_odds = (1 + s.ev_pct_estimated / 100) / s.win_prob_estimated if s.ev_pct_estimated is not None else None
            s.actual_payout = round(100 * implied_odds) if implied_odds else None
        skipped_updated += 1
    if skipped_updated:
        db.commit()

    return {
        "race_id": race_id,
        "actual_result": actual_result,
        "updated_count": len(updated),
        "updated": updated,
        "skipped_updated_count": skipped_updated,
    }


@router.get("/")
def list_races(db: Session = Depends(get_db)):
    # 過去のレース(着順確定済み=actual_resultがある)は、もう投票できないため
    # 選択する必要が無い。一覧からは除外する(のんの要望により変更)。
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.is_(None))
        .order_by(models.Race.id.desc())
        .limit(50)
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
        "actual_result": race.actual_result,
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


@router.delete("/")
def delete_all_races(db: Session = Depends(get_db)):
    """
    レース・選手・オッズ・期待値結果・購入履歴・見送り記録を全て削除する(証拠金残高は対象外)。
    一括DELETE文はSQLAlchemyのcascade設定を経由しないため、外部キーの依存順
    (Purchase/SkippedBet→EvResult/Odds/Entry→Race)に明示的に削除する。
    """
    db.query(models.Purchase).delete()
    db.query(models.SkippedBet).delete()
    db.query(models.EvResult).delete()
    db.query(models.Odds).delete()
    db.query(models.Entry).delete()
    db.query(models.Race).delete()
    db.commit()
    return {"deleted_all": True}


@router.post("/{race_id}/reset-for-reanalysis")
def reset_race_for_reanalysis(race_id: int, db: Session = Depends(get_db)):
    """
    出走表・オッズ・レース結果はそのまま残し、予想に関わるデータ(AI勝率・購入記録・
    見送り記録・EV計算結果)だけを削除して、そのレースを「取り込み直後」の状態に戻す。

    レース取得(スクレイピング)は1日分で数時間かかるため、予想ロジック(AIプロンプト等)を
    変更するたびに新規データを取り直すのは非現実的。既存の出走表・オッズ・結果を
    再利用したまま「予想→投票→結果検証→予想精度確認」のループを再実行できるように
    するために追加した(のんの要望により追加)。

    購入記録の削除に伴い、証拠金への影響(購入時に引かれた分・結果確定で戻った分)を
    正しく巻き戻す。
    """
    from . import bankroll as bankroll_router

    race = db.query(models.Race).filter(models.Race.id == race_id).first()
    if not race:
        raise HTTPException(status_code=404, detail="レースが見つかりません")

    purchases = db.query(models.Purchase).filter(models.Purchase.race_id == race_id).all()
    bankroll_delta = 0.0
    for p in purchases:
        # 購入時に引かれた投票額を戻す
        bankroll_delta += p.stake_amount
        # 結果確定済みで払戻が入っていれば、その分を取り消す
        if p.result != "pending" and p.payout_amount:
            bankroll_delta -= p.payout_amount
    if bankroll_delta:
        bankroll_router.adjust_balance(db, bankroll_delta)

    purchases_deleted = len(purchases)
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
        "bankroll_adjustment": bankroll_delta,
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
