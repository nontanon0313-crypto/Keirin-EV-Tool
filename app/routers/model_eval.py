"""モデル評価API(軽量・読み取り専用・Primaryのみ)。

/model-eval/summary      : 買い目単位の較正 + レース単位のAI vs 市場 + 改善ヒント
/model-eval/ensure-indexes: 集計を速くするインデックスを1回だけ作る(冪等)
"""
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import exists, func, text as sa_text

from .. import models
from ..model_eval_calc import build_hints, race_eval, ticket_eval

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


@router.get("/summary")
def model_eval_summary(
    since: Optional[str] = "calibration_switch",
    races: int = 800,
    prob: str = "ai",
    refresh: bool = False,
):
    """races: レース単位評価に使う直近レース数(上限3000)。prob: ai | blended"""
    from .purchases import _parse_since_param

    races = max(50, min(int(races), 3000))
    prob = "blended" if prob == "blended" else "ai"
    since = since or "calibration_switch"
    key = (since, races, prob)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < _CACHE_TTL:
        return {**hit[1], "cache_hit": True}

    since_dt = _parse_since_param(since) if since != "all" else None
    t0 = time.time()
    s = _primary_session()
    try:
        trows = _ticket_rows(s, since_dt)
        t1 = time.time()
        rrows = _race_rows(s, races, prob)
        t2 = time.time()
    finally:
        try:
            s.close()
        except Exception:
            pass
    ticket = ticket_eval(trows)
    race = race_eval(rrows)
    out = {
        "tier": "primary",
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "race_sample": races,
        "prob_source": prob,
        "ticket": ticket,
        "race": race,
        "hints": build_hints(ticket, race),
        "caveats": [
            "買い目単位は同一レースの複数買い目が相関するため、見かけより有意性が低くなります。",
            "補正係数・混合比率は同じデータで求めた参考値です。反映前に別期間で確認してください。",
            "市場確率は取得済みの3連単オッズから1着車ごとの1/オッズを合計し正規化した値です(控除率の影響は正規化で除去)。",
        ],
        "timing_ms": {"tickets": round((t1 - t0) * 1000), "races": round((t2 - t1) * 1000)},
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
def ensure_indexes():
    """集計を速くするインデックスを作成(既にあれば何もしない)。稼働中でも書き込みを止めない。"""
    from .. import database as dbmod
    if not dbmod._ensure_tier("primary"):
        raise HTTPException(status_code=503, detail="primary が未設定です")
    eng = dbmod._engines["primary"]
    done, errors = [], []
    for name, table, cols in _INDEXES:
        try:
            with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.execute(sa_text("SET statement_timeout = 0"))
                c.execute(sa_text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {cols}"))
            done.append(name)
        except Exception as e:
            errors.append({"index": name, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    return {"ok": not errors, "created_or_existing": done, "errors": errors}
