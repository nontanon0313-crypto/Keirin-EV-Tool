#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase(副系) → Neon(主系) の業務キー・マージ同期。

背景:
  運用が Neon → Supabase(制限中) → Neon と切り替わると、
  Supabase で採番された id が Neon 既存 id と衝突する。
  「MAX(id) より大きい行」「相手に無い id」では取り込めない。

方針:
  - races: external_ref（無ければ venue+R+date）で同一判定
  - 新規レースは Neon で新しい id を採番し、子表は race_id を付け替えて挿入
  - 既存レースでも entries/odds/purchases/skipped は自然キーで欠けていれば追加

使い方:
  python scraper/sync_supabase_to_neon.py
  (DATABASE_URL=Neon, DATABASE_URL_FALLBACK=Supabase)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Optional

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
        print("エラー: DATABASE_URL(Neon) と DATABASE_URL_FALLBACK(Supabase) の両方が必要です。")
        sys.exit(1)
    return (
        create_engine(neon_url, pool_pre_ping=True, connect_args={"connect_timeout": 15}),
        create_engine(sb_url, pool_pre_ping=True, connect_args={"connect_timeout": 15}),
    )


def _row_dict(row) -> dict:
    return dict(row)


def _race_biz_key(r: dict) -> Optional[str]:
    ref = (r.get("external_ref") or "").strip()
    if ref:
        return f"ref:{ref}"
    venue = (r.get("venue_name") or "").strip()
    rn = r.get("race_number")
    rd = r.get("race_date")
    if venue and rn is not None and rd is not None:
        # date only
        try:
            ds = rd.date().isoformat() if hasattr(rd, "date") else str(rd)[:10]
        except Exception:
            ds = str(rd)[:10]
        return f"vd:{venue}|{rn}|{ds}"
    return None


def _insert_returning_id(conn, table: str, data: dict, skip_cols=("id",)) -> int:
    payload = {k: v for k, v in data.items() if k not in skip_cols}
    cols = list(payload.keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING id"
    )
    return conn.execute(sql, payload).scalar()


def _insert_ignore(conn, table: str, data: dict, skip_cols=("id",)) -> bool:
    payload = {k: v for k, v in data.items() if k not in skip_cols}
    cols = list(payload.keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = text(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    )
    # ON CONFLICT DO NOTHING without constraint may fail on PG if no conflict target
    # Use plain insert and catch, or check exists first. Safer: caller checks.
    try:
        conn.execute(
            text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"),
            payload,
        )
        return True
    except Exception:
        return False


def merge_bank_master(sb, neon) -> tuple[dict, int]:
    """name → neon id map. Returns (sb_bank_id → neon_bank_id, inserted)."""
    neon_by_name = {
        r["name"]: r["id"]
        for r in neon.execute(text("SELECT id, name FROM bank_master")).mappings()
    }
    id_map = {}
    inserted = 0
    for r in sb.execute(text("SELECT * FROM bank_master ORDER BY id")).mappings():
        r = _row_dict(r)
        name = r.get("name")
        if name in neon_by_name:
            id_map[r["id"]] = neon_by_name[name]
        else:
            new_id = _insert_returning_id(neon, "bank_master", r)
            neon_by_name[name] = new_id
            id_map[r["id"]] = new_id
            inserted += 1
    return id_map, inserted


def merge_races(sb, neon, bank_map: dict) -> tuple[dict, int]:
    """Returns (sb_race_id → neon_race_id, new_races)."""
    neon_rows = list(neon.execute(text("SELECT * FROM races")).mappings())
    neon_by_key = {}
    for r in neon_rows:
        r = _row_dict(r)
        k = _race_biz_key(r)
        if k:
            neon_by_key[k] = r["id"]

    id_map = {}
    inserted = 0
    for r in sb.execute(text("SELECT * FROM races ORDER BY id")).mappings():
        r = _row_dict(r)
        sb_id = r["id"]
        k = _race_biz_key(r)
        if k and k in neon_by_key:
            id_map[sb_id] = neon_by_key[k]
            continue
        data = dict(r)
        # remap bank_id
        if data.get("bank_id") is not None:
            data["bank_id"] = bank_map.get(data["bank_id"], data["bank_id"])
        new_id = _insert_returning_id(neon, "races", data)
        id_map[sb_id] = new_id
        if k:
            neon_by_key[k] = new_id
        inserted += 1
    return id_map, inserted


def merge_entries(sb, neon, race_map: dict) -> int:
    existing = {
        (r["race_id"], r["car_number"])
        for r in neon.execute(text("SELECT race_id, car_number FROM entries")).mappings()
    }
    inserted = 0
    for r in sb.execute(text("SELECT * FROM entries ORDER BY id")).mappings():
        r = _row_dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r["car_number"])
        if key in existing:
            continue
        data = dict(r)
        data["race_id"] = n_rid
        if _insert_ignore(neon, "entries", data):
            existing.add(key)
            inserted += 1
    return inserted


def merge_odds(sb, neon, race_map: dict) -> int:
    existing = {
        (r["race_id"], r["bet_type"], r["combination"])
        for r in neon.execute(
            text("SELECT race_id, bet_type, combination FROM odds")
        ).mappings()
    }
    inserted = 0
    for r in sb.execute(text("SELECT * FROM odds ORDER BY id")).mappings():
        r = _row_dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r["bet_type"], r["combination"])
        if key in existing:
            continue
        data = dict(r)
        data["race_id"] = n_rid
        if _insert_ignore(neon, "odds", data):
            existing.add(key)
            inserted += 1
    return inserted


def merge_ev_results(sb, neon, race_map: dict) -> tuple[dict, int]:
    """sb_ev_id → neon_ev_id (best effort)."""
    existing = {}
    for r in neon.execute(text("SELECT id, race_id, bet_type, combination FROM ev_results")).mappings():
        r = _row_dict(r)
        existing[(r["race_id"], r.get("bet_type"), r.get("combination"))] = r["id"]
    id_map = {}
    inserted = 0
    try:
        rows = list(sb.execute(text("SELECT * FROM ev_results ORDER BY id")).mappings())
    except Exception as e:
        print(f"  ev_results: スキップ ({e})")
        return {}, 0
    for r in rows:
        r = _row_dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r.get("bet_type"), r.get("combination"))
        if key in existing:
            id_map[r["id"]] = existing[key]
            continue
        data = dict(r)
        data["race_id"] = n_rid
        try:
            new_id = _insert_returning_id(neon, "ev_results", data)
            existing[key] = new_id
            id_map[r["id"]] = new_id
            inserted += 1
        except Exception:
            pass
    return id_map, inserted


def merge_purchases(sb, neon, race_map: dict, ev_map: dict) -> int:
    existing = {
        (r["race_id"], r["bet_type"], r["combination"], r.get("stake_amount"), str(r.get("purchased_at")))
        for r in neon.execute(
            text(
                "SELECT race_id, bet_type, combination, stake_amount, purchased_at FROM purchases"
            )
        ).mappings()
    }
    # 緩め: 同じ race+券種+組 が既にあればスキップ（replay 済みとみなす）
    existing_loose = {
        (r["race_id"], r["bet_type"], r["combination"])
        for r in neon.execute(
            text("SELECT race_id, bet_type, combination FROM purchases")
        ).mappings()
    }
    inserted = 0
    for r in sb.execute(text("SELECT * FROM purchases ORDER BY id")).mappings():
        r = _row_dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        loose = (n_rid, r["bet_type"], r["combination"])
        if loose in existing_loose:
            continue
        data = dict(r)
        data["race_id"] = n_rid
        if data.get("ev_result_id") is not None:
            data["ev_result_id"] = ev_map.get(data["ev_result_id"])
        if _insert_ignore(neon, "purchases", data):
            existing_loose.add(loose)
            inserted += 1
    return inserted


def merge_skipped(sb, neon, race_map: dict) -> int:
    existing = {
        (r["race_id"], r["bet_type"], r["combination"], (r.get("reason") or "")[:80])
        for r in neon.execute(
            text("SELECT race_id, bet_type, combination, reason FROM skipped_bets")
        ).mappings()
    }
    inserted = 0
    for r in sb.execute(text("SELECT * FROM skipped_bets ORDER BY id")).mappings():
        r = _row_dict(r)
        n_rid = race_map.get(r["race_id"])
        if n_rid is None:
            continue
        key = (n_rid, r["bet_type"], r["combination"], (r.get("reason") or "")[:80])
        if key in existing:
            continue
        data = dict(r)
        data["race_id"] = n_rid
        if _insert_ignore(neon, "skipped_bets", data):
            existing.add(key)
            inserted += 1
    return inserted


def sync_bankroll(sb, neon) -> str:
    source_row = sb.execute(
        text(
            "SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1"
        )
    ).mappings().first()
    if source_row is None:
        return "Supabase側にbankroll_stateが無いためスキップ"
    source_row = _row_dict(source_row)
    target_row = neon.execute(
        text(
            "SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1"
        )
    ).mappings().first()
    if target_row is not None:
        target_row = _row_dict(target_row)
        if target_row.get("updated_at") and source_row.get("updated_at"):
            if target_row["updated_at"] >= source_row["updated_at"]:
                return "Neon側の方が新しいため上書きせず"
        neon.execute(
            text(
                "UPDATE bankroll_state SET current_balance=:current_balance, "
                "initial_balance=:initial_balance, updated_at=:updated_at WHERE id=1"
            ),
            source_row,
        )
    else:
        neon.execute(
            text(
                "INSERT INTO bankroll_state (id, current_balance, initial_balance, updated_at) "
                "VALUES (1, :current_balance, :initial_balance, :updated_at)"
            ),
            source_row,
        )
    return f"証拠金を更新 (残高: {source_row['current_balance']}円)"


def main():
    neon_eng, sb_eng = get_engines()
    print(f"[{datetime.now().isoformat()}] Supabase→Neon 業務キー・マージ同期を開始")
    with sb_eng.connect() as sb, neon_eng.connect() as neon:
        try:
            bank_map, n_bank = merge_bank_master(sb, neon)
            neon.commit()
            print(f"  bank_master: 新規 {n_bank} / 対応 {len(bank_map)}")

            race_map, n_race = merge_races(sb, neon, bank_map)
            neon.commit()
            print(f"  races: 新規 {n_race} / 対応 {len(race_map)}")

            n_ent = merge_entries(sb, neon, race_map)
            neon.commit()
            print(f"  entries: 新規 {n_ent}")

            n_odds = merge_odds(sb, neon, race_map)
            neon.commit()
            print(f"  odds: 新規 {n_odds}")

            ev_map, n_ev = merge_ev_results(sb, neon, race_map)
            neon.commit()
            print(f"  ev_results: 新規 {n_ev}")

            n_pur = merge_purchases(sb, neon, race_map, ev_map)
            neon.commit()
            print(f"  purchases: 新規 {n_pur}")

            n_sk = merge_skipped(sb, neon, race_map)
            neon.commit()
            print(f"  skipped_bets: 新規 {n_sk}")

            msg = sync_bankroll(sb, neon)
            neon.commit()
            print(f"  bankroll_state: {msg}")

            total = n_bank + n_race + n_ent + n_odds + n_ev + n_pur + n_sk
            print(f"[{datetime.now().isoformat()}] 完了。新規合計 {total} 件")
        except Exception as e:
            neon.rollback()
            print(f"失敗: {type(e).__name__}: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
