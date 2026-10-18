"""公式3連単払戻金(参考値)まわり。

過去の不具合: 払戻一覧の『7番目=3連単』という決め打ちが9件のページで誤っており、その値で的中の払戻を
上書きしてしまった。/payouts/restore で元(確定オッズ×購入額)へ戻し、誤った公式値を消去する。
"""
from fastapi import APIRouter
from sqlalchemy import func, text as sa_text

from .. import models
from ..payout_calc import restore_value_purchase, restore_value_skipped

router = APIRouter(prefix="/payouts", tags=["payouts"])


def _open_tier(name: str):
    from .. import database as dbmod
    try:
        if dbmod._is_quota_blocked(name):
            return None, "quota_blocked"
    except Exception:
        pass
    try:
        if not dbmod._ensure_tier(name):
            return None, "not_configured"
    except Exception as e:
        return None, type(e).__name__
    s = dbmod._sessions[name]()
    try:
        s.execute(sa_text("SELECT 1"))
        try:
            s.execute(sa_text("ALTER TABLE races ADD COLUMN IF NOT EXISTS payout_3t FLOAT"))
            s.commit()
        except Exception:
            s.rollback()
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return None, type(e).__name__
    return s, None


def _tier_names():
    from .. import database as dbmod
    return list(dbmod._TIER_ORDER)


def _values_update(s, table, rows, set_sql, chunk=400):
    n = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        vals, params = [], {}
        for j, (rid, v) in enumerate(part):
            vals.append(f"(CAST(:i{j} AS INTEGER), CAST(:v{j} AS DOUBLE PRECISION))")
            params[f"i{j}"] = int(rid)
            params[f"v{j}"] = float(v)
        s.execute(sa_text(
            f"UPDATE {table} AS t SET {set_sql} FROM (VALUES {', '.join(vals)}) AS v(id, val) WHERE t.id = v.id"
        ), params)
        n += len(part)
    return n


@router.post("/restore")
def restore(dry_run: bool = True):
    """誤った公式払戻金で書き換えられた購入・見送りの払戻を元へ戻し、races.payout_3t を消去する(全DB)。
    dry_run=true は件数と回収率の前後だけ返す。冪等(2回実行しても同じ)。"""
    P, R, S, O = models.Purchase, models.Race, models.SkippedBet, models.Odds
    out, errors = [], []
    for name in _tier_names():
        s, err = _open_tier(name)
        if s is None:
            if err != "not_configured":
                errors.append({"name": name, "message": err})
            continue
        try:
            stake_all = s.query(func.coalesce(func.sum(P.stake_amount), 0.0)).filter(
                P.result != "pending", P.stake_amount > 0).scalar() or 0.0
            pay_all = s.query(func.coalesce(func.sum(P.payout_amount), 0.0)).filter(
                P.result != "pending", P.stake_amount > 0).scalar() or 0.0
            pfix, delta = [], 0.0
            for pid, st, cur, fo, oap, off in (
                s.query(P.id, P.stake_amount, P.payout_amount, P.final_odds, P.odds_at_purchase, R.payout_3t)
                .join(R, R.id == P.race_id)
                .filter(P.result == "win", P.bet_type == "3連単", R.payout_3t.isnot(None))
                .all()
            ):
                new = restore_value_purchase(st, cur, fo, oap, off)
                if new is not None:
                    pfix.append((pid, new))
                    delta += new - float(cur or 0)
            sk_rows = (
                s.query(S.id, S.race_id, S.combination, S.actual_payout, R.payout_3t)
                .join(R, R.id == S.race_id)
                .filter(S.actual_result == "win", S.bet_type == "3連単", R.payout_3t.isnot(None))
                .all()
            )
            sfix = []
            if sk_rows:
                rids = sorted({r[1] for r in sk_rows})
                odds = {}
                for i in range(0, len(rids), 500):
                    for rid, comb, val in s.query(O.race_id, O.combination, O.odds_value).filter(
                        O.race_id.in_(rids[i:i + 500]), O.bet_type == "3連単"
                    ).all():
                        odds[(rid, comb)] = val
                for sid, rid, comb, cur, off in sk_rows:
                    new = restore_value_skipped(cur, odds.get((rid, comb)), off)
                    if new is not None:
                        sfix.append((sid, new))
            n_official = s.query(func.count(R.id)).filter(R.payout_3t.isnot(None)).scalar() or 0
            rep = {
                "name": name,
                "purchases_to_restore": len(pfix),
                "skipped_to_restore": len(sfix),
                "races_with_official_to_clear": int(n_official),
                "roi_before_pct": round(pay_all / stake_all * 100, 1) if stake_all else None,
                "roi_after_pct": round((pay_all + delta) / stake_all * 100, 1) if stake_all else None,
            }
            if not dry_run:
                if pfix:
                    _values_update(s, "purchases", pfix, "payout_amount = v.val")
                if sfix:
                    _values_update(s, "skipped_bets", sfix, "actual_payout = v.val")
                s.execute(sa_text("UPDATE races SET payout_3t = NULL WHERE payout_3t IS NOT NULL"))
                s.commit()
                rep["applied"] = True
            out.append(rep)
        except Exception as e:
            s.rollback()
            errors.append({"name": name, "message": f"{type(e).__name__}: {str(e)[:150]}"})
        finally:
            s.close()
    if not dry_run:
        try:
            from . import purchases as pr
            pr._purchase_stats_cache["value"] = None
        except Exception:
            pass
    return {"dry_run": dry_run, "tiers": out, "tier_errors": errors}
