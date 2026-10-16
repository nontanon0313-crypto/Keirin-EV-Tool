"""
実資金の収益管理API。

Purchase / SkippedBet / 検証タブの集計とは完全に分離する。
投票プランの想定値と、実際に投票した結果を別フィールドで保持し、
実績データのみを基準に集計・資産推移を返す。
"""
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas

router = APIRouter(prefix="/revenue", tags=["revenue"])


def get_revenue_db():
    """収益(LiveBet)は fallback2 固定。接続できない場合は他系統へ逃がさず 503 を返す。"""  # [fix-20261016]
    from sqlalchemy import text as sa_text
    from .. import database as dbmod
    if not dbmod._ensure_tier("fallback2"):
        raise HTTPException(status_code=503, detail="fallback2(収益DB)が未設定です")
    db = dbmod._sessions["fallback2"]()
    try:
        db.execute(sa_text("SELECT 1"))
    except Exception as e:
        try:
            db.close()
        except Exception:
            pass
        raise HTTPException(
            status_code=503,
            detail=f"fallback2(収益DB)に接続できません: {type(e).__name__}",
        )
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass


def _open_tier_session(name: str):
    """指定系統の session を開く(未作成なら _ensure_tier で作成)。失敗時は None。"""
    from sqlalchemy import text as sa_text
    from .. import database as dbmod
    if not dbmod._ensure_tier(name):
        return None
    s = dbmod._sessions[name]()
    try:
        s.execute(sa_text("SELECT 1"))
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None
    return s


@contextmanager
def _primary_session():
    """レース・購入履歴(Primary)の session。接続不可なら 503。"""
    s = _open_tier_session("primary")
    if s is None:
        raise HTTPException(status_code=503, detail="primary(レース・購入DB)に接続できません")
    try:
        yield s
    finally:
        try:
            s.close()
        except Exception:
            pass


def _force_revenue_tier():
    from .. import database as dbmod
    try:
        dbmod.set_request_tier("fallback2")
    except Exception:
        try:
            dbmod._request_tier.set("fallback2")
        except Exception:
            pass



def _race_meta(db: Session, race_id: int) -> dict:
    """race_id は Primary(日次登録先)の ID 空間。fallback2 の races は参照しない(ID衝突防止)。"""
    with _primary_session() as primary:
        race = primary.query(models.Race).filter(models.Race.id == race_id).first()
        if not race:
            raise HTTPException(status_code=404, detail=f"race_id={race_id} not found (primary)")
        return {
            "race_id": race.id,
            "race_date": race.race_date,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
        }


def _calc_planned_expected_profit(row: models.LiveBet) -> Optional[float]:
    """投票済みデータの想定利益は実投資額を基準に算出する。"""
    actual_stake = row.actual_stake
    if (
        row.vote_status == "voted"
        and actual_stake is not None
        and actual_stake > 0
        and row.planned_win_prob is not None
        and row.planned_odds is not None
    ):
        return actual_stake * (row.planned_win_prob * row.planned_odds - 1.0)

    if (
        row.planned_stake is not None
        and row.planned_stake > 0
        and row.planned_win_prob is not None
        and row.planned_odds is not None
    ):
        return row.planned_stake * (row.planned_win_prob * row.planned_odds - 1.0)

    return None


def _row_to_dict(row: models.LiveBet) -> dict:
    planned_prob = row.planned_win_prob
    planned_odds = row.planned_odds
    planned_ev = row.planned_ev_pct

    actual_stake = (
        row.actual_stake
        if row.actual_stake is not None
        else None
    )
    actual_payout = (
        row.actual_payout
        if row.actual_payout is not None
        else 0.0
    )

    expected_profit = None

    if (
        row.vote_status == "voted"
        and actual_stake is not None
        and actual_stake > 0
        and planned_prob is not None
        and planned_odds is not None
    ):
        expected_profit = actual_stake * (
            planned_prob * planned_odds - 1.0
        )

    actual_pnl = None

    if row.vote_status == "voted" and actual_stake is not None:
        actual_pnl = actual_payout - actual_stake
    elif row.vote_status == "not_voted":
        actual_pnl = 0.0

    return {
        "id": row.id,
        "race_id": row.race_id,
        "race_date": (
            row.race_date.isoformat()
            if row.race_date
            else None
        ),
        "venue_name": row.venue_name,
        "race_number": row.race_number,
        "bet_type": row.bet_type,
        "combination": row.combination,
        "planned_stake": (
            row.planned_stake
            if row.planned_stake is not None
            else None
        ),
        "planned_win_prob": planned_prob,
        "planned_odds": planned_odds,
        "planned_ev_pct": planned_ev,
        "planned_expected_profit": (
            round(expected_profit, 2)
            if expected_profit is not None
            else None
        ),
        "vote_status": row.vote_status,
        "actual_stake": actual_stake,
        "actual_result": row.actual_result,
        "actual_payout": actual_payout,
        "actual_pnl": (
            round(actual_pnl, 2)
            if actual_pnl is not None
            else None
        ),
        "memo": row.memo,
        "source": row.source,
        "created_at": (
            row.created_at.isoformat()
            if row.created_at
            else None
        ),
        "updated_at": (
            row.updated_at.isoformat()
            if row.updated_at
            else None
        ),
    }

@router.post("/from-plan")
def register_from_plan(payload: schemas.LiveBetFromPlanCreate, db: Session = Depends(get_revenue_db)):
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
        p_stake = it.planned_stake
        p_wp = it.planned_win_prob
        p_odds = it.planned_odds
        # 0-1 と 0-100 の両方が来ても正規化
        if p_wp is not None and float(p_wp) > 1.0:
            p_wp = float(p_wp) / 100.0
        if planned_exp is None and p_stake and p_wp is not None and p_odds is not None:
            planned_exp = float(p_stake) * (float(p_wp) * float(p_odds) - 1.0)
        row = models.LiveBet(
            race_id=None,  # Primary の ID 空間のため fallback2 には保存しない
            race_date=meta["race_date"],
            venue_name=meta["venue_name"],
            race_number=meta["race_number"],
            bet_type=it.bet_type,
            combination=it.combination,
            planned_stake=p_stake,
            planned_win_prob=p_wp,
            planned_odds=p_odds,
            planned_ev_pct=it.planned_ev_pct,
            planned_expected_profit=planned_exp,
            vote_status="voted" if mark_voted else "planned",
            actual_stake=(p_stake if mark_voted else None),
            actual_result="pending",
            actual_payout=0.0,
            source="from_plan",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
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
    db: Session = Depends(get_revenue_db),
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
def mark_win(live_bet_id: int, payload: schemas.LiveBetWinUpdate, db: Session = Depends(get_revenue_db)):
    """的中1件。払戻だけ書いて win。実額未設定なら予定額。"""
    row = db.query(models.LiveBet).filter(models.LiveBet.id == live_bet_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="live_bet not found")
    row.vote_status = "voted"
    if payload.planned_stake is not None:
        row.planned_stake = payload.planned_stake

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
def create_manual(payload: schemas.LiveBetManualCreate, db: Session = Depends(get_revenue_db)):
    """投票タブ「購入記録」(または手入力)を実績として fallback2 の LiveBet に保存する。

    race / purchase は Primary から読む。LiveBet.race_id には Primary の ID を保存しない。
    """
    marker = f"購入#{payload.purchase_id}" if payload.purchase_id else None
    if marker:
        dup = db.query(models.LiveBet).filter(models.LiveBet.memo.like(f"%{marker}%")).first()
        if dup is not None:
            return _row_to_dict(dup)  # 二重送信は同じ行を返す

    race_date = payload.race_date
    venue_name = payload.venue_name
    race_number = payload.race_number
    purchase = None
    ev_stake = None
    if payload.purchase_id:
        with _primary_session() as primary:
            purchase = (
                primary.query(models.Purchase)
                .filter(models.Purchase.id == payload.purchase_id)
                .first()
            )
            if purchase is None:
                raise HTTPException(status_code=404, detail=f"purchase_id={payload.purchase_id} not found (primary)")
            if purchase.ev_result_id:
                ev_row = (
                    primary.query(models.EvResult)
                    .filter(models.EvResult.id == purchase.ev_result_id)
                    .first()
                )
                if ev_row is not None:
                    ev_stake = ev_row.recommended_stake
            # purchase を session 外で参照するため値を確定
            p_race_id = purchase.race_id
            p_wp = purchase.win_prob_at_purchase
            p_odds = purchase.odds_at_purchase
            p_ev = purchase.ev_pct_at_purchase
        if payload.race_id and int(payload.race_id) != int(p_race_id):
            raise HTTPException(status_code=400, detail="race_id と purchase_id のレースが一致しません")
        meta_race_id = payload.race_id or p_race_id
        if payload.planned_stake is None and ev_stake is not None:
            payload.planned_stake = ev_stake
        if payload.planned_win_prob is None:
            payload.planned_win_prob = p_wp
        if payload.planned_odds is None:
            payload.planned_odds = p_odds
        if payload.planned_ev_pct is None:
            payload.planned_ev_pct = p_ev
    else:
        meta_race_id = payload.race_id

    if meta_race_id:
        meta = _race_meta(db, meta_race_id)
        race_date = race_date or meta["race_date"]
        venue_name = venue_name or meta["venue_name"]
        race_number = race_number if race_number is not None else meta["race_number"]

    wp = payload.planned_win_prob
    if wp is not None and float(wp) > 1.0:
        wp = float(wp) / 100.0
    planned_exp = None
    if (
        payload.actual_stake is not None
        and payload.actual_stake > 0
        and wp is not None
        and payload.planned_odds is not None
    ):
        planned_exp = payload.actual_stake * (float(wp) * float(payload.planned_odds) - 1.0)

    memo = payload.memo
    if marker:
        memo = f"{memo} / {marker}" if memo else marker

    row = models.LiveBet(
        race_id=None,  # Primary の race_id は fallback2 の races と ID 空間が異なる(FK/取り違え防止)
        race_date=race_date,
        venue_name=venue_name,
        race_number=race_number,
        bet_type=payload.bet_type,
        combination=payload.combination,
        planned_stake=payload.planned_stake,
        planned_win_prob=wp,
        planned_odds=payload.planned_odds,
        planned_ev_pct=payload.planned_ev_pct,
        planned_expected_profit=planned_exp,
        vote_status=payload.vote_status or "voted",
        actual_stake=payload.actual_stake,
        actual_result=payload.actual_result or "pending",
        actual_payout=payload.actual_payout or 0.0,
        memo=memo,
        source="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_dict(row)


@router.put("/{live_bet_id}")
def update_live_bet(live_bet_id: int, payload: schemas.LiveBetUpdate, db: Session = Depends(get_revenue_db)):
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

    # 想定利益は実投資額(actual_stake)を基準にDBへ永続化する。
    if (
        row.actual_stake is not None
        and row.actual_stake > 0
        and row.planned_win_prob is not None
        and row.planned_odds is not None
    ):
        row.planned_expected_profit = row.actual_stake * (
            row.planned_win_prob * row.planned_odds - 1.0
        )
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
def delete_live_bet(live_bet_id: int, db: Session = Depends(get_revenue_db)):
    row = db.query(models.LiveBet).filter(models.LiveBet.id == live_bet_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="live_bet not found")
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": live_bet_id}




def _normalize_combo(comb: str):
    """312 -> {312, 3-1-2} / 3-1-2 -> {3-1-2, 312}"""
    if not comb:
        return set()
    c = str(comb).strip()
    out = {c}
    if "-" in c:
        out.add(c.replace("-", ""))
    elif c.isdigit() and len(c) >= 3:
        out.add("-".join(list(c)))
    return out

@router.post("/backfill-planned")
def backfill_planned(dry_run: bool = True, db: Session = Depends(get_revenue_db)):
    """planned_* 欠損を Primary の Purchase から埋める(LiveBet は fallback2)。"""
    from datetime import datetime, timedelta

    def norm_combos(comb: str):
        return _normalize_combo(comb)

    rows = db.query(models.LiveBet).filter(models.LiveBet.vote_status == "voted").all()
    targets = [
        lb for lb in rows
        if not (
            lb.planned_win_prob is not None
            and lb.planned_odds is not None
            and lb.planned_expected_profit is not None
        )
    ]
    updated, skipped = [], []
    with _primary_session() as pdb:
        def pick_purchase(race_ids, bet_type, combos):
            if not race_ids or not combos:
                return None
            q = pdb.query(models.Purchase).filter(
                models.Purchase.race_id.in_(list(race_ids)),
                models.Purchase.combination.in_(list(combos)),
            )
            if bet_type:
                q = q.filter(models.Purchase.bet_type == bet_type)
            found = q.order_by(models.Purchase.id.desc()).all()
            for r in found:
                if r.win_prob_at_purchase is not None and r.odds_at_purchase is not None:
                    return r
            for r in found:
                if r.win_prob_at_purchase is not None or r.odds_at_purchase is not None:
                    return r
            return found[0] if found else None

        for lb in targets:
            combos = norm_combos(lb.combination)
            race_ids = set()
            if lb.venue_name and lb.race_number is not None:
                rq = pdb.query(models.Race).filter(
                    models.Race.venue_name == lb.venue_name,
                    models.Race.race_number == int(lb.race_number),
                )
                if lb.race_date is not None:
                    d0 = lb.race_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    rq = rq.filter(models.Race.race_date >= d0, models.Race.race_date < d0 + timedelta(days=1))
                for race in rq.all():
                    race_ids.add(int(race.id))
            puch = pick_purchase(race_ids, lb.bet_type, combos)
            if puch is None:
                skipped.append({
                    "id": lb.id, "venue": lb.venue_name, "R": lb.race_number,
                    "combination": lb.combination, "reason": "no_purchase",
                })
                continue
            before = (lb.planned_win_prob, lb.planned_odds, lb.planned_expected_profit)
            wp = puch.win_prob_at_purchase
            if wp is not None:
                wp = float(wp)
                if wp > 1.0:
                    wp = wp / 100.0
                lb.planned_win_prob = wp
            if puch.odds_at_purchase is not None:
                lb.planned_odds = float(puch.odds_at_purchase)
            stake = lb.actual_stake if lb.actual_stake is not None else lb.planned_stake
            if lb.planned_stake is None and stake is not None:
                lb.planned_stake = float(stake)
            if lb.planned_win_prob is not None and lb.planned_odds is not None and stake:
                lb.planned_expected_profit = float(stake) * (
                    float(lb.planned_win_prob) * float(lb.planned_odds) - 1.0
                )
            if lb.combination and "-" not in str(lb.combination) and str(lb.combination).isdigit():
                lb.combination = "-".join(list(str(lb.combination)))
            lb.updated_at = datetime.utcnow()
            after = (lb.planned_win_prob, lb.planned_odds, lb.planned_expected_profit)
            if after != before:
                updated.append({
                    "id": lb.id,
                    "combination": lb.combination,
                    "planned_win_prob": lb.planned_win_prob,
                    "planned_odds": lb.planned_odds,
                    "planned_expected_profit": lb.planned_expected_profit,
                    "from_purchase_id": puch.id,
                    "from_race_id": puch.race_id,
                })
            else:
                skipped.append({
                    "id": lb.id, "combination": lb.combination,
                    "reason": "purchase_found_but_empty_fields", "purchase_id": puch.id,
                })
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return {
        "dry_run": dry_run,
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated[:50],
        "skipped": skipped[:30],
    }


@router.get("/list")
def list_live_bets(
    race_id: Optional[int] = None,
    limit: int = Query(200, ge=1, le=1000),
    days: int = Query(2, ge=1, le=365, description="直近何日分(既定2。当日・前日)"),
    db: Session = Depends(get_revenue_db),
):
    """収益一覧。fallback2 のみ。"""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import or_, and_

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    start_jst = now_jst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(0, days - 1))
    start_utc = start_jst.astimezone(timezone.utc).replace(tzinfo=None)
    start_date_naive = start_jst.replace(tzinfo=None)

    q = db.query(models.LiveBet)
    if race_id is not None:
        q = q.filter(models.LiveBet.race_id == race_id)
    q = q.filter(
        or_(
            and_(models.LiveBet.race_date.isnot(None), models.LiveBet.race_date >= start_date_naive),
            and_(models.LiveBet.created_at.isnot(None), models.LiveBet.created_at >= start_utc),
        )
    )
    rows = q.order_by(
        models.LiveBet.created_at.desc().nullslast(),
        models.LiveBet.id.desc(),
    ).limit(limit).all()
    return {
        "count": len(rows),
        "days": days,
        "since_jst": start_jst.isoformat(),
        "items": [_row_to_dict(r) for r in rows],
        "merged_tiers": False,
    }



def _iter_sorted(db: Session) -> List[models.LiveBet]:
    return (
        db.query(models.LiveBet)
        .order_by(models.LiveBet.race_date.asc().nullslast(), models.LiveBet.created_at.asc(), models.LiveBet.id.asc())
        .all()
    )


def _iter_sorted_all_readable_tiers() -> List[models.LiveBet]:
    """読める系統を1つずつ開き LiveBet を合算（同時オープンしない・OOM対策）。

    同一内容が複数系統に複製されている場合は件数が増える点に注意。
    収益の正本を1DBに寄せる運用が望ましい。
    """
    from .. import database as dbmod
    from sqlalchemy import text as sa_text
    import gc

    all_rows: List[models.LiveBet] = []
    for name in dbmod._TIER_ORDER:
        if name not in dbmod._sessions:
            continue
        if dbmod._is_quota_blocked(name):
            continue
        session = None
        try:
            session = dbmod._sessions[name]()
            session.execute(sa_text("SELECT 1"))
            try:
                dbmod._clear_cooldown(name)
            except Exception:
                pass
            rows = (
                session.query(models.LiveBet)
                .order_by(
                    models.LiveBet.race_date.asc().nullslast(),
                    models.LiveBet.created_at.asc(),
                    models.LiveBet.id.asc(),
                )
                .all()
            )
            # セッションclose後も属性を読めるよう expunge
            for r in rows:
                session.expunge(r)
                # 系統名を後から参照できるよう印を付ける
                try:
                    r._tier_name = name  # type: ignore[attr-defined]
                except Exception:
                    pass
            all_rows.extend(rows)
        except Exception:
            try:
                if session is not None:
                    dbmod._mark_cooldown(name, Exception("revenue tier open failed"))
            except Exception:
                pass
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            gc.collect()

    all_rows.sort(
        key=lambda r: (
            r.race_date is None,
            r.race_date or 0,
            r.created_at or 0,
            r.id or 0,
        )
    )
    return all_rows


@router.get("/stats")
def revenue_stats(db: Session = Depends(get_revenue_db)):
    _force_revenue_tier()
    """
    収益タブの集計。
    投資額は実投資額を唯一の基準とする。
    読める全系統の LiveBet を合算する。
    """
    rows = _iter_sorted(db)

    expected_profit_sum = 0.0
    expected_hit_prob_sum = 0.0
    expected_count = 0

    actual_stake_sum = 0.0
    actual_payout_sum = 0.0
    actual_pnl_sum = 0.0

    voted_count = 0
    hit_count = 0
    not_voted_count = 0
    pending_count = 0

    odds_sum = 0.0
    odds_count = 0

    for r in rows:
        if r.vote_status == "not_voted":
            not_voted_count += 1
            continue

        if r.vote_status != "voted":
            if r.actual_result == "pending":
                pending_count += 1
            continue

        stake = (
            r.actual_stake
            if r.actual_stake is not None
            else 0.0
        )

        payout = (
            r.actual_payout
            if r.actual_payout is not None
            else 0.0
        )

        actual_stake_sum += stake
        actual_payout_sum += payout
        actual_pnl_sum += payout - stake
        voted_count += 1

        if r.planned_odds is not None:
            odds_sum += r.planned_odds
            odds_count += 1

        if r.actual_result == "win":
            hit_count += 1
        elif r.actual_result == "pending":
            pending_count += 1

        if (
            stake > 0
            and r.planned_win_prob is not None
            and r.planned_odds is not None
        ):
            expected_profit_sum += stake * (
                r.planned_win_prob * r.planned_odds - 1.0
            )
            expected_hit_prob_sum += r.planned_win_prob
            expected_count += 1

    expected_hit_rate = (
        expected_hit_prob_sum / expected_count * 100.0
        if expected_count
        else None
    )

    expected_roi = (
        (
            (actual_stake_sum + expected_profit_sum)
            / actual_stake_sum
        ) * 100.0
        if actual_stake_sum > 0
        else None
    )

    actual_hit_rate = (
        hit_count / voted_count * 100.0
        if voted_count
        else None
    )

    actual_roi = (
        actual_payout_sum / actual_stake_sum * 100.0
        if actual_stake_sum > 0
        else None
    )

    def _diff(expected, actual):
        if expected is None or actual is None:
            return None
        return round(actual - expected, 2)

    return {
        "total_rows": len(rows),
        "planned": {
            "expected_profit": round(expected_profit_sum, 0),
            "hit_rate_pct": (
                round(expected_hit_rate, 2)
                if expected_hit_rate is not None
                else None
            ),
            "roi_pct": (
                round(expected_roi, 2)
                if expected_roi is not None
                else None
            ),
            "count": expected_count,
        },
        "actual": {
            "stake": round(actual_stake_sum, 0),
            "payout": round(actual_payout_sum, 0),
            "pnl": round(actual_pnl_sum, 0),
            "hit_rate_pct": (
                round(actual_hit_rate, 2)
                if actual_hit_rate is not None
                else None
            ),
            "roi_pct": (
                round(actual_roi, 2)
                if actual_roi is not None
                else None
            ),
            "voted_count": voted_count,
            "hit_count": hit_count,
            "not_voted_count": not_voted_count,
            "pending_count": pending_count,
            "avg_odds": (
                round(odds_sum / odds_count, 2)
                if odds_count
                else None
            ),
        },
        "diff": {
            "pnl": _diff(
                expected_profit_sum,
                actual_pnl_sum,
            ),
            "hit_rate_pct": _diff(
                expected_hit_rate,
                actual_hit_rate,
            ),
            "roi_pct": _diff(
                expected_roi,
                actual_roi,
            ),
        },
    }

@router.get("/equity-curve")
def equity_curve(db: Session = Depends(get_revenue_db)):
    _force_revenue_tier()
    """
    収益タブ用の資産推移。
    投資額の基準は実投資額(actual_stake)のみとし、
    想定系列も同じ実投資額を横軸にして、購入時の想定勝率・オッズから
    想定損益を累積する。
    読める全系統の LiveBet を合算する。
    """
    rows = _iter_sorted(db)

    points_actual = [
        {
            "cum_stake": 0.0,
            "cum_pnl": 0.0,
            "assets": 0.0,
        }
    ]
    points_planned = [
        {
            "cum_stake": 0.0,
            "cum_pnl": 0.0,
            "assets": 0.0,
        }
    ]

    cum_stake = 0.0
    cum_pnl = 0.0
    cum_expected_pnl = 0.0

    for r in rows:
        if (
            r.vote_status != "voted"
            or r.actual_stake is None
        ):
            continue

        stake = r.actual_stake
        payout = (
            r.actual_payout
            if r.actual_payout is not None
            else 0.0
        )

        cum_stake += stake
        cum_pnl += payout - stake

        if (
            stake > 0
            and r.planned_win_prob is not None
            and r.planned_odds is not None
        ):
            cum_expected_pnl += stake * (
                r.planned_win_prob * r.planned_odds - 1.0
            )

        point = {
            "cum_stake": round(cum_stake, 0),
            "cum_pnl": round(cum_pnl, 0),
            "assets": round(cum_pnl, 0),
            "id": r.id,
            "result": r.actual_result,
            "label": (
                f"{r.venue_name or ''} "
                f"{r.race_number or ''}R "
                f"{r.bet_type} {r.combination}"
            ),
        }
        points_actual.append(point)

        points_planned.append({
            "cum_stake": round(cum_stake, 0),
            "cum_pnl": round(cum_expected_pnl, 0),
            "assets": round(cum_expected_pnl, 0),
            "id": r.id,
            "result": r.actual_result,
            "label": (
                f"{r.venue_name or ''} "
                f"{r.race_number or ''}R "
                f"{r.bet_type} {r.combination}"
            ),
        })

    return {
        "actual": points_actual,
        "planned": points_planned,
        "final_actual_pnl": round(cum_pnl, 0),
        "final_actual_stake": round(cum_stake, 0),
        "final_expected_pnl": round(cum_expected_pnl, 0),
        "final_assets": round(cum_pnl, 0),
    }


@router.post("/admin/migrate-to-fallback2")
def migrate_live_bets_to_fallback2(
    dry_run: bool = True,
    token: str = "",
):
    """primary/fallback の live_bets を fallback2 に集約。Render 上でのみ実行。"""
    import os
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    from .. import database as dbmod

    expected = (os.environ.get("ADMIN_SYNC_SECRET") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_SYNC_SECRET が未設定です")
    if token != expected:
        raise HTTPException(status_code=403, detail="トークンが違います")

    def biz_key(r):
        rd = r.race_date.isoformat() if r.race_date else ""
        return (
            (r.venue_name or "").strip(),
            int(r.race_number or 0),
            rd,
            (r.bet_type or "").strip(),
            (r.combination or "").strip(),
            (r.vote_status or "").strip(),
            (r.actual_result or "").strip(),
            round(float(r.actual_stake or 0.0), 2),
            round(float(r.planned_stake or 0.0), 2),
            (r.source or "").strip(),
            round(float(r.planned_odds or 0.0), 4),
            round(float(r.planned_win_prob or 0.0), 6),
        )

    def open_tier(name):
        return _open_tier_session(name)  # _ensure_tier + _sessions[name]()

    def stats(rows):
        voted = [r for r in rows if r.vote_status == "voted"]
        hits = [r for r in voted if r.actual_result == "win"]
        stake = sum(float(r.actual_stake or 0) for r in voted)
        payout = sum(float(r.actual_payout or 0) for r in voted)
        exp = 0.0
        for r in voted:
            st = float(r.actual_stake or 0)
            if st > 0 and r.planned_win_prob is not None and r.planned_odds is not None:
                exp += st * (float(r.planned_win_prob) * float(r.planned_odds) - 1.0)
        return {
            "n": len(rows),
            "voted": len(voted),
            "hits": len(hits),
            "hit_rate_pct": round(len(hits) / len(voted) * 100, 2) if voted else None,
            "stake": round(stake, 0),
            "payout": round(payout, 0),
            "pnl": round(payout - stake, 0),
            "expected_profit": round(exp, 0),
        }

    dst = open_tier("fallback2")
    if dst is None:
        raise HTTPException(500, "fallback2 に接続できません")
    sources_info = {}
    to_add = []
    try:
        existing = dst.query(models.LiveBet).all()
        keys = {biz_key(r) for r in existing}
        before = stats(existing)
        for name in ("primary", "fallback"):
            s = open_tier(name)
            if s is None:
                sources_info[name] = {"error": "no session"}
                continue
            try:
                rows = s.query(models.LiveBet).all()
                sources_info[name] = stats(rows)
                for r in rows:
                    k = biz_key(r)
                    if k in keys:
                        continue
                    keys.add(k)
                    to_add.append(r)
            finally:
                s.close()

        if dry_run:
            return {
                "dry_run": True,
                "before_fallback2": before,
                "sources": sources_info,
                "to_insert": len(to_add),
            }

        for r in to_add:
            nb = models.LiveBet(
                race_id=None,
                race_date=r.race_date,
                venue_name=r.venue_name,
                race_number=r.race_number,
                bet_type=r.bet_type,
                combination=r.combination,
                planned_stake=r.planned_stake,
                planned_win_prob=r.planned_win_prob,
                planned_odds=r.planned_odds,
                planned_ev_pct=r.planned_ev_pct,
                planned_expected_profit=r.planned_expected_profit,
                vote_status=r.vote_status,
                actual_stake=r.actual_stake,
                actual_result=r.actual_result,
                actual_payout=r.actual_payout if r.actual_payout is not None else 0.0,
                memo=r.memo,
                source=r.source,
                created_at=r.created_at or datetime.utcnow(),
                updated_at=r.updated_at or datetime.utcnow(),
            )
            dst.add(nb)
        dst.commit()
        after_rows = dst.query(models.LiveBet).all()
        return {
            "dry_run": False,
            "before_fallback2": before,
            "sources": sources_info,
            "inserted": len(to_add),
            "after_fallback2": stats(after_rows),
        }
    finally:
        dst.close()


@router.post("/admin/backfill-from-purchases")
def backfill_from_purchases(
    date: Optional[str] = Query(None, description="購入日(JST) YYYY-MM-DD"),
    hour_from: int = Query(0, ge=0, le=23, description="JST開始時(含む)"),
    hour_to: int = Query(24, ge=1, le=24, description="JST終了時(含まない)"),
    purchase_ids: str = Query("", description="カンマ区切りの購入ID。指定時は日時窓より優先"),
    dry_run: bool = True,
    token: str = "",
    db: Session = Depends(get_revenue_db),
):
    """Primary の Purchase を fallback2 の LiveBet に載せ直す(再購入不要)。  # [fix-20261016]

    日次スクリプト由来の Purchase と投票タブ由来を自動判別できないため、
    必ず dry_run で候補を確認し、purchase_ids または日時窓で対象を絞ること。
    """
    import os
    from datetime import timedelta

    # トークン不要(冪等・追加のみ・Purchaseの複製だけで破壊的操作なし)。token は互換のため受け取るだけ。
    ids = [int(x) for x in purchase_ids.replace(" ", "").split(",") if x]
    if not ids and not date:
        raise HTTPException(status_code=400, detail="date か purchase_ids を指定してください")

    def combo_key(c):
        return "".join(ch for ch in str(c or "") if ch.isdigit())

    cands = []
    with _primary_session() as pdb:
        q = pdb.query(models.Purchase)
        if ids:
            q = q.filter(models.Purchase.id.in_(ids))
        else:
            try:
                d0 = datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="date は YYYY-MM-DD")
            jst_start = d0 + timedelta(hours=hour_from)
            jst_end = d0 + timedelta(hours=hour_to)
            q = q.filter(
                models.Purchase.purchased_at >= jst_start - timedelta(hours=9),
                models.Purchase.purchased_at < jst_end - timedelta(hours=9),
            )
        for p in q.order_by(models.Purchase.id.asc()).all():
            race = pdb.query(models.Race).filter(models.Race.id == p.race_id).first()
            ev_stake = None
            if p.ev_result_id:
                ev_row = pdb.query(models.EvResult).filter(models.EvResult.id == p.ev_result_id).first()
                if ev_row is not None:
                    ev_stake = ev_row.recommended_stake
            cands.append({
                "purchase_id": p.id,
                "purchased_at": p.purchased_at,
                "race_date": race.race_date if race else None,
                "venue_name": race.venue_name if race else None,
                "race_number": race.race_number if race else None,
                "bet_type": p.bet_type,
                "combination": p.combination,
                "stake": p.stake_amount,
                "odds": p.odds_at_purchase,
                "wp": p.win_prob_at_purchase,
                "ev_pct": p.ev_pct_at_purchase,
                "ev_stake": ev_stake,
                "result": p.result or "pending",
                "payout": p.payout_amount or 0.0,
            })

    existing = db.query(models.LiveBet).all()
    memo_hit = set()
    fuzzy = set()
    for lb in existing:
        for c in cands:
            if lb.memo and f"購入#{c['purchase_id']}" in lb.memo:
                memo_hit.add(c["purchase_id"])
        if lb.race_date is not None:
            fuzzy.add((
                (lb.venue_name or "").strip(), int(lb.race_number or 0), lb.race_date.date(),
                (lb.bet_type or "").strip(), combo_key(lb.combination), round(float(lb.actual_stake or 0)),
            ))

    report, to_add = [], []
    for c in cands:
        fk = None
        if c["race_date"] is not None:
            fk = (
                (c["venue_name"] or "").strip(), int(c["race_number"] or 0), c["race_date"].date(),
                (c["bet_type"] or "").strip(), combo_key(c["combination"]), round(float(c["stake"] or 0)),
            )
        if c["purchase_id"] in memo_hit:
            status = "already(memo)"
        elif fk is not None and fk in fuzzy:
            status = "already(same_bet)"
        elif c["race_date"] is None:
            status = "skip(race_not_found)"
        else:
            status = "to_insert"
            to_add.append(c)
        report.append({
            "purchase_id": c["purchase_id"],
            "purchased_at_jst": (c["purchased_at"] + timedelta(hours=9)).isoformat() if c["purchased_at"] else None,
            "race": f'{c["race_date"].date() if c["race_date"] else "?"} {c["venue_name"]} {c["race_number"]}R',
            "bet": f'{c["bet_type"]} {c["combination"]}',
            "stake": c["stake"],
            "result": c["result"],
            "status": status,
        })

    if dry_run:
        return {"dry_run": True, "found": len(cands), "to_insert": len(to_add), "items": report}

    for c in to_add:
        wp = c["wp"]
        if wp is not None and float(wp) > 1.0:
            wp = float(wp) / 100.0
        exp = None
        if c["stake"] and wp is not None and c["odds"] is not None:
            exp = float(c["stake"]) * (float(wp) * float(c["odds"]) - 1.0)
        res = c["result"] if c["result"] in ("pending", "win", "lose") else "pending"
        db.add(models.LiveBet(
            race_id=None,
            race_date=c["race_date"],
            venue_name=c["venue_name"],
            race_number=c["race_number"],
            bet_type=c["bet_type"],
            combination=c["combination"],
            planned_stake=c["ev_stake"],
            planned_win_prob=wp,
            planned_odds=c["odds"],
            planned_ev_pct=c["ev_pct"],
            planned_expected_profit=exp,
            vote_status="voted",
            actual_stake=c["stake"],
            actual_result=res,
            actual_payout=float(c["payout"]) if res == "win" else 0.0,
            memo=f"購入#{c['purchase_id']}",
            source="manual",
            created_at=c["purchased_at"] or datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
    db.commit()
    return {"dry_run": False, "found": len(cands), "inserted": len(to_add), "items": report}
