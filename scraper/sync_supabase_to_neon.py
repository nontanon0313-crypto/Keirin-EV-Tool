#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase → Neon 業務キー・マージ（高速版）。

id の話:
  - シーケンスが揃っていれば、副系の新規は MAX(Neon)+1 以降になり「重複しない」。
  - 同期が中途半端だと、同じ id に別内容が入ることがある（衝突）。
  - どちらでも拾えるよう、external_ref 等の業務キーでマージする。

速度:
  - 一括 INSERT
  - odds は「新規レース」と「Neon に1件も odds が無い既存レース」だけ
  - purchases/skipped は race 対応後に欠け分のみ
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


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
        print("エラー: DATABASE_URL と DATABASE_URL_FALLBACK が必要です")
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


def _bulk_insert(conn, table: str, rows: list[dict], skip_cols=frozenset({"id"})) -> int:
    if not rows:
        return 0
    cols = [c for c in rows[0].keys() if c not in skip_cols]
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})")
    payload = [{c: row.get(c) for c in cols} for row in rows]
    # chunk
    n = 0
    for i in range(0, len(payload), 200):
        chunk = payload[i : i + 200]
        conn.execute(sql, chunk)
        n += len(chunk)
    return n


def _insert_one_returning(conn, table: str, data: dict, skip_cols=frozenset({"id"})) -> int:
    payload = {k: v for k, v in data.items() if k not in skip_cols}
    cols = list(payload.keys())
    sql = text(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':'+c for c in cols)}) RETURNING id"
    )
    return conn.execute(sql, payload).scalar()


def main():
    neon_eng, sb_eng = get_engines()
    print(f"[{datetime.now().isoformat()}] Supabase→Neon マージ開始", flush=True)

    with sb_eng.connect() as sb, neon_eng.connect() as neon:
        # --- bank ---
        neon_banks = {
            r["name"]: r["id"]
            for r in neon.execute(text("SELECT id, name FROM bank_master")).mappings()
        }
        bank_map = {}
        n_bank = 0
        for r in sb.execute(text("SELECT * FROM bank_master")).mappings():
            r = dict(r)
            if r["name"] in neon_banks:
                bank_map[r["id"]] = neon_banks[r["name"]]
            else:
                new_id = _insert_one_returning(neon, "bank_master", r)
                neon_banks[r["name"]] = new_id
                bank_map[r["id"]] = new_id
                n_bank += 1
        neon.commit()
        print(f"  bank_master: 新規{n_bank}", flush=True)

        # --- races ---
        neon_by_key = {}
        neon_race_ids = set()
        for r in neon.execute(
            text("SELECT id, external_ref, venue_name, race_number, race_date FROM races")
        ).mappings():
            r = dict(r)
            neon_race_ids.add(r["id"])
            k = _race_key(r)
            if k:
                neon_by_key[k] = r["id"]

        race_map = {}
        new_race_sb_ids = []
        n_race = 0
        for r in sb.execute(text("SELECT * FROM races ORDER BY id")).mappings():
            r = dict(r)
            sb_id = r["id"]
            k = _race_key(r)
            if k and k in neon_by_key:
                race_map[sb_id] = neon_by_key[k]
                continue
            data = dict(r)
            if data.get("bank_id") is not None:
                data["bank_id"] = bank_map.get(data["bank_id"], data["bank_id"])
            new_id = _insert_one_returning(neon, "races", data)
            race_map[sb_id] = new_id
            if k:
                neon_by_key[k] = new_id
            new_race_sb_ids.append(sb_id)
            n_race += 1
        neon.commit()
        print(f"  races: 新規{n_race} / 対応{len(race_map)}", flush=True)

        new_neon_race_ids = {race_map[s] for s in new_race_sb_ids}

        # Neon に odds が1件も無いレース（既存だが空）も埋める
        neon_with_odds = {
            r[0]
            for r in neon.execute(text("SELECT DISTINCT race_id FROM odds")).fetchall()
        }
        fill_odds_neon_ids = set(new_neon_race_ids)
        for sb_id, n_id in race_map.items():
            if n_id not in neon_with_odds:
                fill_odds_neon_ids.add(n_id)
        # reverse: neon_id -> list of sb race ids (usually 1)
        neon_to_sb = {}
        for sb_id, n_id in race_map.items():
            neon_to_sb.setdefault(n_id, []).append(sb_id)

        # --- entries (new races only, plus races with 0 entries on neon) ---
        neon_entry_keys = {
            (r["race_id"], r["car_number"])
            for r in neon.execute(text("SELECT race_id, car_number FROM entries")).mappings()
        }
        neon_with_entries = {k[0] for k in neon_entry_keys}
        entry_rows = []
        for r in sb.execute(text("SELECT * FROM entries")).mappings():
            r = dict(r)
            n_rid = race_map.get(r["race_id"])
            if n_rid is None:
                continue
            if (n_rid, r["car_number"]) in neon_entry_keys:
                continue
            if n_rid not in new_neon_race_ids and n_rid in neon_with_entries:
                continue
            r["race_id"] = n_rid
            entry_rows.append(r)
        n_ent = _bulk_insert(neon, "entries", entry_rows)
        neon.commit()
        print(f"  entries: 新規{n_ent}", flush=True)

        # --- odds: only for races needing fill ---
        need_sb_race_ids = set()
        for n_id in fill_odds_neon_ids:
            need_sb_race_ids.update(neon_to_sb.get(n_id, []))
        n_odds = 0
        if need_sb_race_ids:
            # existing keys only for those neon races
            existing_odds = {
                (r["race_id"], r["bet_type"], r["combination"])
                for r in neon.execute(
                    text(
                        "SELECT race_id, bet_type, combination FROM odds "
                        "WHERE race_id = ANY(:ids)"
                    ),
                    {"ids": list(fill_odds_neon_ids)},
                ).mappings()
            }
            odds_buf = []
            # fetch sb odds in chunks by sb race id
            sb_ids = list(need_sb_race_ids)
            for i in range(0, len(sb_ids), 50):
                chunk_ids = sb_ids[i : i + 50]
                for r in sb.execute(
                    text("SELECT * FROM odds WHERE race_id = ANY(:ids)"),
                    {"ids": chunk_ids},
                ).mappings():
                    r = dict(r)
                    n_rid = race_map.get(r["race_id"])
                    if n_rid is None:
                        continue
                    key = (n_rid, r["bet_type"], r["combination"])
                    if key in existing_odds:
                        continue
                    r["race_id"] = n_rid
                    odds_buf.append(r)
                    existing_odds.add(key)
                    if len(odds_buf) >= 300:
                        n_odds += _bulk_insert(neon, "odds", odds_buf)
                        neon.commit()
                        odds_buf = []
            n_odds += _bulk_insert(neon, "odds", odds_buf)
            neon.commit()
        print(f"  odds: 新規{n_odds}", flush=True)

        # --- purchases ---
        existing_pur = {
            (r["race_id"], r["bet_type"], r["combination"])
            for r in neon.execute(
                text("SELECT race_id, bet_type, combination FROM purchases")
            ).mappings()
        }
        pur_buf = []
        n_pur = 0
        for r in sb.execute(text("SELECT * FROM purchases")).mappings():
            r = dict(r)
            n_rid = race_map.get(r["race_id"])
            if n_rid is None:
                continue
            key = (n_rid, r["bet_type"], r["combination"])
            if key in existing_pur:
                continue
            r["race_id"] = n_rid
            r["ev_result_id"] = None  # FK は付けない（衝突回避）
            pur_buf.append(r)
            existing_pur.add(key)
            if len(pur_buf) >= 200:
                n_pur += _bulk_insert(neon, "purchases", pur_buf)
                neon.commit()
                pur_buf = []
        n_pur += _bulk_insert(neon, "purchases", pur_buf)
        neon.commit()
        print(f"  purchases: 新規{n_pur}", flush=True)

        # --- skipped ---
        existing_sk = {
            (r["race_id"], r["bet_type"], r["combination"], (r.get("reason") or "")[:60])
            for r in neon.execute(
                text("SELECT race_id, bet_type, combination, reason FROM skipped_bets")
            ).mappings()
        }
        sk_buf = []
        n_sk = 0
        for r in sb.execute(text("SELECT * FROM skipped_bets")).mappings():
            r = dict(r)
            n_rid = race_map.get(r["race_id"])
            if n_rid is None:
                continue
            key = (n_rid, r["bet_type"], r["combination"], (r.get("reason") or "")[:60])
            if key in existing_sk:
                continue
            r["race_id"] = n_rid
            sk_buf.append(r)
            existing_sk.add(key)
            if len(sk_buf) >= 200:
                n_sk += _bulk_insert(neon, "skipped_bets", sk_buf)
                neon.commit()
                sk_buf = []
        n_sk += _bulk_insert(neon, "skipped_bets", sk_buf)
        neon.commit()
        print(f"  skipped_bets: 新規{n_sk}", flush=True)

        # bankroll: 新しい方
        sb_b = sb.execute(
            text(
                "SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id=1"
            )
        ).mappings().first()
        if sb_b:
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
                print(f"  bankroll: insert {sb_b['current_balance']}", flush=True)
            else:
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
                    print(f"  bankroll: update {sb_b['current_balance']}", flush=True)
                else:
                    print("  bankroll: Neon側が新しいためスキップ", flush=True)
            neon.commit()

        total = n_bank + n_race + n_ent + n_odds + n_pur + n_sk
        print(f"[{datetime.now().isoformat()}] 完了 新規合計={total}", flush=True)


if __name__ == "__main__":
    main()
