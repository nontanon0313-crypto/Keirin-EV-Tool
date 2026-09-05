#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase → Neon 段階マージ。

Render の HTTP は長時間で 502 になるため、ステップ分割する。

  python scraper/sync_supabase_to_neon.py banks
  python scraper/sync_supabase_to_neon.py races
  python scraper/sync_supabase_to_neon.py entries
  python scraper/sync_supabase_to_neon.py odds
  python scraper/sync_supabase_to_neon.py purchases
  python scraper/sync_supabase_to_neon.py skipped
  python scraper/sync_supabase_to_neon.py bankroll
  python scraper/sync_supabase_to_neon.py all   # ローカル用・一括

環境変数: DATABASE_URL=Neon, DATABASE_URL_FALLBACK=Supabase
odds は ODDS_LIMIT (既定 80) レースずつ。繰り返し呼ぶと続きを処理。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

STEPS = ("banks", "races", "entries", "odds", "purchases", "skipped", "bankroll")


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "channel_binding=" in url:
        base, _, q = url.partition("?")
        qs = "&".join(x for x in q.split("&") if x and not x.startswith("channel_binding"))
        url = base + (("?" + qs) if qs else "")
    return url


def get_engines():
    neon_url = _normalize_url(os.environ.get("DATABASE_URL", ""))
    sb_url = _normalize_url(
        os.environ.get("DATABASE_URL_FALLBACK", "")
        or os.environ.get("DATABASE_URL_SECONDARY", "")
    )
    if not neon_url or not sb_url:
        print("エラー: DATABASE_URL / DATABASE_URL_FALLBACK が必要")
        sys.exit(1)
    kw = dict(pool_pre_ping=True, connect_args={"connect_timeout": 30})
    return create_engine(neon_url, **kw), create_engine(sb_url, **kw)


def _race_key(r: dict) -> Optional[str]:
    ref = (r.get("external_ref") or "").strip()
    if ref:
        return f"ref:{ref}"
    venue = (r.get("venue_name") or "").strip()
    rn = r.get("race_number")
    rd = r.get("race_date")
    if venue and rn is not None and rd is not None:
        try:
            ds = rd.date().isoformat() if hasattr(rd, "date") else str(rd)[:10]
        except Exception:
            ds = str(rd)[:10]
        return f"vd:{venue}|{rn}|{ds}"
    return None


def _bulk_insert(conn, table: str, rows: list, skip_cols=frozenset({"id"})) -> int:
    if not rows:
        return 0
    cols = [c for c in rows[0].keys() if c not in skip_cols]
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':'+c for c in cols)})"
    )
    n = 0
    for i in range(0, len(rows), 150):
        chunk = [{c: row.get(c) for c in cols} for row in rows[i : i + 150]]
        conn.execute(sql, chunk)
        n += len(chunk)
    return n


def _insert_one(conn, table: str, data: dict, skip_cols=frozenset({"id"})) -> int:
    payload = {k: v for k, v in data.items() if k not in skip_cols}
    cols = list(payload.keys())
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':'+c for c in cols)}) RETURNING id"
    )
    return conn.execute(sql, payload).scalar()


def build_race_map(sb, neon):
    neon_by_key = {}
    for r in neon.execute(
        text("SELECT id, external_ref, venue_name, race_number, race_date FROM races")
    ).mappings():
        r = dict(r)
        k = _race_key(r)
        if k:
            neon_by_key[k] = r["id"]
    race_map = {}
    for r in sb.execute(
        text("SELECT id, external_ref, venue_name, race_number, race_date FROM races")
    ).mappings():
        r = dict(r)
        k = _race_key(r)
        if k and k in neon_by_key:
            race_map[r["id"]] = neon_by_key[k]
    return race_map, neon_by_key


def step_banks(sb, neon):
    neon_banks = {
        r["name"]: r["id"]
        for r in neon.execute(text("SELECT id, name FROM bank_master")).mappings()
    }
    n = 0
    for r in sb.execute(text("SELECT * FROM bank_master")).mappings():
        r = dict(r)
        if r["name"] in neon_banks:
            continue
        new_id = _insert_one(neon, "bank_master", r)
        neon_banks[r["name"]] = new_id
        n += 1
    neon.commit()
    print(f"banks: 新規{n}", flush=True)
    return n


def step_races(sb, neon):
    neon_banks = {
        r["name"]: r["id"]
        for r in neon.execute(text("SELECT id, name FROM bank_master")).mappings()
    }
    sb_banks = {
        r["id"]: r["name"]
        for r in sb.execute(text("SELECT id, name FROM bank_master")).mappings()
    }
    neon_by_key = {}
    for r in neon.execute(
        text("SELECT id, external_ref, venue_name, race_number, race_date FROM races")
    ).mappings():
        r = dict(r)
        k = _race_key(r)
        if k:
            neon_by_key[k] = r["id"]
    n = 0
    for r in sb.execute(text("SELECT * FROM races ORDER BY id")).mappings():
        r = dict(r)
        k = _race_key(r)
        if k and k in neon_by_key:
            continue
        if r.get("bank_id") is not None:
            bname = sb_banks.get(r["bank_id"])
            if bname and bname in neon_banks:
                r["bank_id"] = neon_banks[bname]
        new_id = _insert_one(neon, "races", r)
        if k:
            neon_by_key[k] = new_id
        n += 1
        if n % 50 == 0:
            neon.commit()
            print(f"  races progress {n}", flush=True)
    neon.commit()
    print(f"races: 新規{n}", flush=True)
    return n


def step_entries(sb, neon):
    race_map, _ = build_race_map(sb, neon)
    existing = {
        (r["race_id"], r["car_number"])
        for r in neon.execute(text("SELECT race_id, car_number FROM entries")).mappings()
    }
    buf, n = [], 0
    for r in sb.execute(text("SELECT * FROM entries")).mappings():
        r = dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        if (n_rid, r["car_number"]) in existing:
            continue
        r["race_id"] = n_rid
        buf.append(r)
        existing.add((n_rid, r["car_number"]))
        if len(buf) >= 150:
            n += _bulk_insert(neon, "entries", buf)
            neon.commit()
            buf = []
    n += _bulk_insert(neon, "entries", buf)
    neon.commit()
    print(f"entries: 新規{n}", flush=True)
    return n


def step_odds(sb, neon):
    """Neon に odds が無いレースだけ、最大 ODDS_LIMIT 件ずつ。"""
    limit = int(os.environ.get("ODDS_LIMIT", "60"))
    race_map, _ = build_race_map(sb, neon)
    neon_with_odds = {
        r[0] for r in neon.execute(text("SELECT DISTINCT race_id FROM odds")).fetchall()
    }
    # neon race ids that need odds, that we can fill from sb
    need = []
    for sb_id, n_id in race_map.items():
        if n_id not in neon_with_odds:
            need.append((sb_id, n_id))
    need.sort(key=lambda x: x[1])
    batch = need[:limit]
    print(f"odds: 要埋め{len(need)}レース中 今回{len(batch)}", flush=True)
    if not batch:
        print("odds: 完了(残り0)", flush=True)
        return 0
    n = 0
    for sb_id, n_id in batch:
        existing = {
            (r["bet_type"], r["combination"])
            for r in neon.execute(
                text(
                    "SELECT bet_type, combination FROM odds WHERE race_id=:rid"
                ),
                {"rid": n_id},
            ).mappings()
        }
        buf = []
        for r in sb.execute(
            text("SELECT * FROM odds WHERE race_id=:rid"), {"rid": sb_id}
        ).mappings():
            r = dict(r)
            key = (r["bet_type"], r["combination"])
            if key in existing:
                continue
            r["race_id"] = n_id
            buf.append(r)
            existing.add(key)
        n += _bulk_insert(neon, "odds", buf)
        neon.commit()
    remain = max(0, len(need) - len(batch))
    print(f"odds: 新規行{n} 残りレース約{remain}", flush=True)
    return n


def step_purchases(sb, neon):
    """id 順に ROW_LIMIT 件ずつ（AFTER_ID から再開）。"""
    after_id = int(os.environ.get("AFTER_ID", "0"))
    limit = int(os.environ.get("ROW_LIMIT", "800"))
    race_map, _ = build_race_map(sb, neon)
    rows = list(
        sb.execute(
            text(
                "SELECT * FROM purchases WHERE id > :a ORDER BY id LIMIT :lim"
            ),
            {"a": after_id, "lim": limit},
        ).mappings()
    )
    if not rows:
        print("purchases: 完了 残り0 last_id=%s" % after_id, flush=True)
        return 0
    neon_rids = {race_map[r["race_id"]] for r in rows if r["race_id"] in race_map}
    existing = set()
    if neon_rids:
        existing = {
            (r["race_id"], r["bet_type"], r["combination"])
            for r in neon.execute(
                text(
                    "SELECT race_id, bet_type, combination FROM purchases "
                    "WHERE race_id = ANY(:ids)"
                ),
                {"ids": list(neon_rids)},
            ).mappings()
        }
    buf, n = [], 0
    for r in rows:
        r = dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r["bet_type"], r["combination"])
        if key in existing:
            continue
        r["race_id"] = n_rid
        r["ev_result_id"] = None
        buf.append(r)
        existing.add(key)
        if len(buf) >= 150:
            n += _bulk_insert(neon, "purchases", buf)
            neon.commit()
            buf = []
    n += _bulk_insert(neon, "purchases", buf)
    neon.commit()
    last_id = rows[-1]["id"]
    print(
        f"purchases: 新規{n} last_id={last_id} batch={len(rows)} "
        f"{'続きあり' if len(rows) >= limit else 'この帯は完了'}",
        flush=True,
    )
    return n


def step_skipped(sb, neon):
    """id 順に ROW_LIMIT 件ずつ（AFTER_ID から再開）。skipped は件数が多い。"""
    after_id = int(os.environ.get("AFTER_ID", "0"))
    limit = int(os.environ.get("ROW_LIMIT", "500"))
    race_map, _ = build_race_map(sb, neon)
    rows = list(
        sb.execute(
            text(
                "SELECT * FROM skipped_bets WHERE id > :a ORDER BY id LIMIT :lim"
            ),
            {"a": after_id, "lim": limit},
        ).mappings()
    )
    if not rows:
        print("skipped: 完了 残り0 last_id=%s" % after_id, flush=True)
        return 0
    neon_rids = {race_map[r["race_id"]] for r in rows if r["race_id"] in race_map}
    existing = set()
    if neon_rids:
        existing = {
            (r["race_id"], r["bet_type"], r["combination"], (r.get("reason") or "")[:60])
            for r in neon.execute(
                text(
                    "SELECT race_id, bet_type, combination, reason FROM skipped_bets "
                    "WHERE race_id = ANY(:ids)"
                ),
                {"ids": list(neon_rids)},
            ).mappings()
        }
    buf, n = [], 0
    for r in rows:
        r = dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r["bet_type"], r["combination"], (r.get("reason") or "")[:60])
        if key in existing:
            continue
        r["race_id"] = n_rid
        buf.append(r)
        existing.add(key)
        if len(buf) >= 150:
            n += _bulk_insert(neon, "skipped_bets", buf)
            neon.commit()
            buf = []
    n += _bulk_insert(neon, "skipped_bets", buf)
    neon.commit()
    last_id = rows[-1]["id"]
    print(
        f"skipped: 新規{n} last_id={last_id} batch={len(rows)} "
        f"{'続きあり' if len(rows) >= limit else 'この帯は完了'}",
        flush=True,
    )
    return n


def step_bankroll(sb, neon):
    sb_b = sb.execute(
        text(
            "SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id=1"
        )
    ).mappings().first()
    if not sb_b:
        print("bankroll: SBに無し", flush=True)
        return 0
    sb_b = dict(sb_b)
    nb = neon.execute(
        text(
            "SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id=1"
        )
    ).mappings().first()
    if nb is None:
        neon.execute(
            text(
                "INSERT INTO bankroll_state (id, current_balance, initial_balance, updated_at) "
                "VALUES (1, :current_balance, :initial_balance, :updated_at)"
            ),
            sb_b,
        )
        neon.commit()
        print(f"bankroll: insert {sb_b['current_balance']}", flush=True)
        return 1
    nb = dict(nb)
    if not nb.get("updated_at") or (
        sb_b.get("updated_at") and sb_b["updated_at"] > nb["updated_at"]
    ):
        neon.execute(
            text(
                "UPDATE bankroll_state SET current_balance=:current_balance, "
                "initial_balance=:initial_balance, updated_at=:updated_at WHERE id=1"
            ),
            sb_b,
        )
        neon.commit()
        print(f"bankroll: update {sb_b['current_balance']}", flush=True)
        return 1
    print("bankroll: skip (Neon newer)", flush=True)
    return 0


def main():
    step = (sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        os.environ["AFTER_ID"] = sys.argv[2]
    print(
        f"[{datetime.now().isoformat()}] step={step} AFTER_ID={os.environ.get('AFTER_ID','0')}",
        flush=True,
    )
    neon_eng, sb_eng = get_engines()
    with sb_eng.connect() as sb, neon_eng.connect() as neon:
        if step == "all":
            for s in STEPS:
                if s == "odds":
                    # odds は繰り返し
                    while True:
                        n = step_odds(sb, neon)
                        if n == 0:
                            # check remain
                            race_map, _ = build_race_map(sb, neon)
                            neon_with = {
                                r[0]
                                for r in neon.execute(
                                    text("SELECT DISTINCT race_id FROM odds")
                                ).fetchall()
                            }
                            remain = sum(1 for _, nid in race_map.items() if nid not in neon_with)
                            if remain == 0:
                                break
                        # continue loop
                else:
                    globals()[f"step_{s}"](sb, neon)
        elif step in STEPS:
            globals()[f"step_{step}"](sb, neon)
        else:
            print(f"未知のstep: {step} 有効: {STEPS} all")
            sys.exit(1)
    print(f"[{datetime.now().isoformat()}] step={step} done", flush=True)


if __name__ == "__main__":
    main()
