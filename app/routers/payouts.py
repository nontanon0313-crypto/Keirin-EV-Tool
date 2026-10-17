"""公式払戻金(3連単)の保存と、既存の的中を公式払戻金で精算し直すAPI(全DB合算・冪等)。

POST /payouts/backfill   : [{external_ref, payout_3t}] を races.payout_3t に保存
POST /payouts/resettle   : dry_run=true で影響(回収率の前後)だけ確認 → dry_run=false で反映
"""
from statistics import median

from fastapi import APIRouter
from sqlalchemy import func, text as sa_text

from .. import models

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


def _values_update(s, table, cols_sql, rows, set_sql, chunk=400):
    """rows: [(id, value)] を VALUES 付きUPDATE 1本/チャンクで書く。"""
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


@router.post("/backfill")
def backfill_payouts(payload: dict):
    items = payload.get("items") or []
    want = {}
    for it in items:
        ref = str(it.get("external_ref") or "")
        try:
            v = float(it.get("payout_3t"))
        except (TypeError, ValueError):
            continue
        if ref and v >= 100:
            want[ref] = v
    refs = list(want)
    out, errors = [], []
    for name in _tier_names():
        s, err = _open_tier(name)
        if s is None:
            if err != "not_configured":
                errors.append({"name": name, "message": err})
            continue
        try:
            found = updated = 0
            todo = []
            for i in range(0, len(refs), 500):
                chunk = refs[i:i + 500]
                for rid, ref, cur in (
                    s.query(models.Race.id, models.Race.external_ref, models.Race.payout_3t)
                    .filter(models.Race.external_ref.in_(chunk)).all()
                ):
                    found += 1
                    if cur is None or abs(cur - want[ref]) > 0.5:
                        todo.append((rid, want[ref]))
            if todo:
                updated = _values_update(s, "races", "", todo, "payout_3t = v.val")
                s.commit()
            out.append({"name": name, "races_found": found, "updated": updated})
        except Exception as e:
            s.rollback()
            errors.append({"name": name, "message": f"{type(e).__name__}: {str(e)[:150]}"})
        finally:
            s.close()
    return {"received": len(want), "tiers": out, "tier_errors": errors}


def _resettle_tier(s, apply: bool):
    P, R, S = models.Purchase, models.Race, models.SkippedBet
    wins = (
        s.query(P.id, P.race_id, P.stake_amount, P.payout_amount, R.payout_3t)
        .join(R, R.id == P.race_id)
        .filter(P.result == "win", P.bet_type == "3連単", R.payout_3t.isnot(None))
        .all()
    )
    n_win_all = (
        s.query(func.count(P.id)).filter(P.result == "win", P.bet_type == "3連単").scalar() or 0
    )
    stake_all = (
        s.query(func.coalesce(func.sum(P.stake_amount), 0.0))
        .filter(P.result != "pending", P.stake_amount > 0).scalar() or 0.0
    )
    pay_all = (
        s.query(func.coalesce(func.sum(P.payout_amount), 0.0))
        .filter(P.result != "pending", P.stake_amount > 0).scalar() or 0.0
    )
    old = new = 0.0
    upd, ratios, worst = [], [], []
    for pid, rid, st, po, off in wins:
        newp = round(float(st or 0) * float(off) / 100.0)
        old += float(po or 0)
        new += newp
        upd.append((pid, newp))
        if newp > 0:
            ratios.append(float(po or 0) / newp)
        worst.append((float(po or 0) - newp, rid, float(st or 0), float(po or 0), newp))
    worst.sort(reverse=True)
    pay_after = pay_all - old + new
    rep = {
        "wins_total": int(n_win_all),
        "wins_with_official": len(wins),
        "coverage_pct": round(len(wins) / n_win_all * 100, 1) if n_win_all else None,
        "stake_total": round(stake_all),
        "payout_before": round(pay_all),
        "payout_after": round(pay_after),
        "roi_before_pct": round(pay_all / stake_all * 100, 1) if stake_all else None,
        "roi_after_pct": round(pay_after / stake_all * 100, 1) if stake_all else None,
        "stored_over_official_ratio": (
            {
                "median": round(median(ratios), 3),
                "p10": round(sorted(ratios)[int(len(ratios) * 0.10)], 3),
                "p90": round(sorted(ratios)[min(len(ratios) - 1, int(len(ratios) * 0.90))], 3),
                "share_over_1_2_pct": round(sum(1 for r in ratios if r > 1.2) / len(ratios) * 100, 1),
            } if ratios else None
        ),
        "biggest_overstatements": [
            {"race_id": r, "stake": st, "stored_payout": po, "official_payout": npay}
            for _, r, st, po, npay in worst[:5]
        ],
    }
    if apply and upd:
        _values_update(s, "purchases", "", upd, "payout_amount = v.val")
        sk = (
            s.query(S.id, R.payout_3t)
            .join(R, R.id == S.race_id)
            .filter(S.actual_result == "win", S.bet_type == "3連単", R.payout_3t.isnot(None))
            .all()
        )
        if sk:
            _values_update(s, "skipped_bets", "", [(i, round(o)) for i, o in sk], "actual_payout = v.val")
        s.commit()
        rep["applied"] = True
    return rep


@router.post("/resettle")
def resettle(dry_run: bool = True):
    out, errors = [], []
    tot = {"stake": 0.0, "before": 0.0, "after": 0.0, "wins": 0, "wins_official": 0}
    for name in _tier_names():
        s, err = _open_tier(name)
        if s is None:
            if err != "not_configured":
                errors.append({"name": name, "message": err})
            continue
        try:
            rep = _resettle_tier(s, apply=not dry_run)
            rep["name"] = name
            out.append(rep)
            tot["stake"] += rep["stake_total"]
            tot["before"] += rep["payout_before"]
            tot["after"] += rep["payout_after"]
            tot["wins"] += rep["wins_total"]
            tot["wins_official"] += rep["wins_with_official"]
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
    return {
        "dry_run": dry_run,
        "total": {
            "stake": round(tot["stake"]), "payout_before": round(tot["before"]), "payout_after": round(tot["after"]),
            "roi_before_pct": round(tot["before"] / tot["stake"] * 100, 1) if tot["stake"] else None,
            "roi_after_pct": round(tot["after"] / tot["stake"] * 100, 1) if tot["stake"] else None,
            "wins": tot["wins"], "wins_with_official": tot["wins_official"],
        },
        "tiers": out,
        "tier_errors": errors,
    }


@router.get("/missing-wins")
def missing_wins():
    """的中(3連単)があるのに公式払戻金が未保存のレースの external_ref 一覧(全DB)。"""
    P, R = models.Purchase, models.Race
    out, errors = [], []
    for name in _tier_names():
        s, err = _open_tier(name)
        if s is None:
            if err != "not_configured":
                errors.append({"name": name, "message": err})
            continue
        try:
            rows = (
                s.query(R.id, R.external_ref)
                .join(P, P.race_id == R.id)
                .filter(P.result == "win", P.bet_type == "3連単", R.payout_3t.is_(None))
                .distinct()
                .all()
            )
            out.append({
                "name": name,
                "refs": [ref for _, ref in rows if ref],
                "no_external_ref": sum(1 for _, ref in rows if not ref),
            })
        except Exception as e:
            s.rollback()
            errors.append({"name": name, "message": f"{type(e).__name__}: {str(e)[:150]}"})
        finally:
            s.close()
    return {"tiers": out, "tier_errors": errors}
