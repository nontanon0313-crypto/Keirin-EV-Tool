"""モデル評価API(軽量・読み取り専用・Primaryのみ)。

/model-eval/summary      : 買い目単位の較正 + レース単位のAI vs 市場 + 改善ヒント
/model-eval/ensure-indexes: 集計を速くするインデックスを1回だけ作る(冪等)
"""
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import exists, func, text as sa_text

from .. import models
from ..model_eval_calc import build_hints, race_eval, roi_hints, roi_report, ticket_eval

router = APIRouter(prefix="/model-eval", tags=["model-eval"])

_CACHE = {}
_CACHE_TTL = 600  # 秒


def _primary_session():
    from .. import database as dbmod
    if not dbmod._ensure_tier("primary"):
        raise HTTPException(status_code=503, detail="primary(レース・購入DB)が未設定です")
    s = dbmod._sessions["primary"]()
    try:
        s.execute(sa_text("SELECT 1"))
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"primary に接続できません: {type(e).__name__}")
    return s


def _ticket_rows(s, since_dt):
    P, S = models.Purchase, models.SkippedBet
    pq = s.query(
        P.win_prob_at_purchase, P.result, P.stake_amount, P.payout_amount,
        P.odds_at_purchase, P.ev_pct_at_purchase,
    ).filter(P.result != "pending")
    if since_dt is not None:
        pq = pq.filter(P.purchased_at >= since_dt)
    rows = [
        (wp, 1 if res == "win" else 0, float(st or 0), float(po or 0), od, ev)
        for wp, res, st, po, od, ev in pq.all()
    ]
    sq = s.query(S.win_prob_estimated, S.actual_result, S.ev_pct_estimated).filter(
        S.actual_result.isnot(None)
    )
    if since_dt is not None:
        sq = sq.filter(S.created_at >= since_dt)
    rows += [(wp, 1 if res == "win" else 0, 0.0, 0.0, None, ev) for wp, res, ev in sq.all()]
    return rows


def _roi_rows(s, since_dt, tier):
    """実購入の1件ごと: (cluster, stake, payout, win, timing, p, odds)。timing は購入が発走前(live)か結果確定後(replay)か。"""
    from datetime import timedelta
    P, R = models.Purchase, models.Race
    q = (
        s.query(P.race_id, P.stake_amount, P.payout_amount, P.result, P.win_prob_at_purchase,
                P.odds_at_purchase, P.purchased_at, R.post_time, R.race_date)
        .join(R, R.id == P.race_id)
        .filter(P.result != "pending", P.stake_amount > 0)
    )
    if since_dt is not None:
        q = q.filter(P.purchased_at >= since_dt)
    rows = []
    for rid, st, po, res, wp, od, pat, post, rdate in q.all():
        ref = post or (rdate + timedelta(days=1) if rdate else None)
        if pat is None or ref is None:
            timing = "unknown"
        else:
            timing = "live" if (pat + timedelta(hours=9)) < ref else "replay"
        rows.append(((tier, rid), float(st or 0), float(po or 0), res == "win", timing, wp, od))
    return rows


def _race_rows(s, n_races: int, prob: str):
    R, E, O = models.Race, models.Entry, models.Odds
    pcol = E.blended_win_prob if prob == "blended" else E.ai_win_prob
    q = (
        s.query(R.id, R.actual_result)
        .filter(R.actual_result.isnot(None), R.actual_result != "")
        .filter(exists().where((E.race_id == R.id) & (pcol.isnot(None))))
        .order_by(R.id.desc())
        .limit(n_races)
    )
    races = {}
    for rid, ar in q.all():
        try:
            winner = int(str(ar).split("-")[0])
        except (ValueError, IndexError):
            continue
        races[rid] = {"winner": winner, "model": {}, "market": {}}
    if not races:
        return []
    ids = list(races)
    for rid, car, pr in (
        s.query(E.race_id, E.car_number, pcol).filter(E.race_id.in_(ids), pcol.isnot(None)).all()
    ):
        races[rid]["model"][car] = float(pr)
    first = func.substr(O.combination, 1, 1)
    for rid, car, w in (
        s.query(O.race_id, first, func.sum(1.0 / O.odds_value))
        .filter(O.race_id.in_(ids), O.bet_type == "3連単", O.odds_value > 0)
        .group_by(O.race_id, first)
        .all()
    ):
        try:
            races[rid]["market"][int(car)] = float(w)
        except (TypeError, ValueError):
            pass
    return list(races.values())


def _tier_names(tiers: str):
    from .. import database as dbmod
    if tiers == "primary":
        return ["primary"]
    return list(dbmod._TIER_ORDER)


def _open_tier(name: str):
    """系統の session を開く。使えない系統は None(理由つき)。"""
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
        return None, f"{type(e).__name__}"
    s = dbmod._sessions[name]()
    try:
        s.execute(sa_text("SELECT 1"))
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return None, f"{type(e).__name__}"
    return s, None


@router.get("/summary")
def model_eval_summary(
    since: Optional[str] = "calibration_switch",
    races: int = 800,
    prob: str = "ai",
    tiers: str = "all",
    refresh: bool = False,
):
    """仕様: 検証系の集計は既定で全DB合算(サンプル数確保)。tiers=primary は調査用。
    races: レース単位評価に使う直近レース数(系統ごと・上限3000)。prob: ai | blended"""
    from .purchases import _parse_since_param

    races = max(50, min(int(races), 3000))
    prob = "blended" if prob == "blended" else "ai"
    tiers = "primary" if tiers == "primary" else "all"
    since = since or "calibration_switch"
    key = (since, races, prob, tiers)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < _CACHE_TTL:
        return {**hit[1], "cache_hit": True}

    since_dt = _parse_since_param(since) if since != "all" else None
    t0 = time.time()
    trows, rrows, used, errors, roirows = [], [], [], [], []
    for name in _tier_names(tiers):
        s, err = _open_tier(name)
        if s is None:
            if err != "not_configured":
                errors.append({"name": name, "message": err})
            continue
        try:
            tr = _ticket_rows(s, since_dt)
            rr = _race_rows(s, races, prob)
            roirows += _roi_rows(s, since_dt, name)
            trows += tr
            rrows += rr
            used.append({"name": name, "ticket_rows": len(tr), "race_rows": len(rr)})
        except Exception as e:
            errors.append({"name": name, "message": f"{type(e).__name__}: {str(e)[:120]}"})
        finally:
            try:
                s.close()
            except Exception:
                pass
    if not used:
        raise HTTPException(status_code=503, detail=f"読めるDBがありません: {errors}")
    t1 = time.time()
    ticket = ticket_eval(trows)
    race = race_eval(rrows)
    roi = roi_report(roirows)
    hints = build_hints(ticket, race) + roi_hints(roi)
    out = {
        "roi_check": roi,
        "tiers_used": [u["name"] for u in used],
        "tiers": used,
        "tier_errors": errors,
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "race_sample_per_tier": races,
        "prob_source": prob,
        "ticket": ticket,
        "race": race,
        "hints": hints,
        "caveats": [
            "実運用(発走前)=購入の記録時刻が発走時刻より前。再投票(結果確定後)=過去レースを現行ロジックでやり直した分で、閾値をこのデータに合わせて決めた場合は楽観的になります。",
            "回収率の払戻は『保存済みオッズ×金額』で計算されており、公式の払戻金ではありません(早い時刻のオッズは大穴ほど高く出やすい)。",
            "全DB合算の集計です(系統ごとにrace_idが別番号のため、レースは系統内で評価して合算しています)。",
            "買い目単位は同一レースの複数買い目が相関するため、見かけより有意性が低くなります。",
            "補正係数・混合比率は同じデータで求めた参考値です。反映前に別期間で確認してください。",
            "市場確率は取得済みの3連単オッズから1着車ごとの1/オッズを合計し正規化した値です。",
        ],
        "timing_ms": {"total": round((t1 - t0) * 1000)},
        "cache_hit": False,
    }
    _CACHE[key] = (now, out)
    return out


_INDEXES = [
    ("ix_odds_race_bet", "odds", "(race_id, bet_type)"),
    ("ix_entries_race_id", "entries", "(race_id)"),
    ("ix_skipped_bets_created_at", "skipped_bets", "(created_at)"),
    ("ix_purchases_purchased_at", "purchases", "(purchased_at)"),
    ("ix_races_actual_result_id", "races", "(id) WHERE actual_result IS NOT NULL"),
]


@router.post("/ensure-indexes")
def ensure_indexes(tiers: str = "all"):
    """集計を速くするインデックスを作成(既にあれば何もしない)。稼働中でも書き込みを止めない。"""
    from .. import database as dbmod
    done, errors = [], []
    for name in _tier_names("primary" if tiers == "primary" else "all"):
        try:
            if not dbmod._ensure_tier(name):
                continue
        except Exception:
            continue
        eng = dbmod._engines.get(name)
        if eng is None:
            continue
        for ix, table, cols in _INDEXES:
            try:
                with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                    c.execute(sa_text("SET statement_timeout = 0"))
                    c.execute(sa_text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {ix} ON {table} {cols}"))
                done.append(f"{name}:{ix}")
            except Exception as e:
                errors.append({"tier": name, "index": ix, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    return {"ok": not errors, "created_or_existing": done, "errors": errors}
