#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabase(副系)→Neon(主系)への差分同期スクリプト。

scraper/sync_neon_to_supabase.py の逆方向版。
Neonの制限が解除されて主系に戻す時に、Supabase運用中に増えた分の投票記録・
レースデータをNeon側にも反映するために使う。

流れ:
1. Neonの制限中は DATABASE_PREFER=fallback (Supabase運用)
2. Neonの制限が解除されたら、まずこのスクリプトを実行してSupabase→Neonへ
   差分を反映する
3. その後Renderの環境変数 DATABASE_PREFER=primary に戻す

差分同期の考え方・シーケンスの合わせ直し等はsync_neon_to_supabase.pyと同じ
(そちらのdocstringを参照)。こちらはNeonの通信量を気にする必要は無いが、
念のため同じ差分方式で統一している(全件洗い替えだと、Neon運用中に既に
入っていた行を誤って上書き・重複させるリスクがあるため)。

使い方:
    python scraper/sync_supabase_to_neon.py
    (DATABASE_URL=Neon, DATABASE_URL_FALLBACK=Supabase を環境変数から読む。
     .envに設定済みならそのまま実行するだけでよい)
"""
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

TABLES_IN_ORDER = [
    "bank_master",
    "races",
    "entries",
    "odds",
    "ev_results",
    "purchases",
    "skipped_bets",
]


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def get_engines():
    neon_url = _normalize_url(os.environ.get("DATABASE_URL", ""))
    supabase_url = _normalize_url(
        os.environ.get("DATABASE_URL_FALLBACK", "") or os.environ.get("DATABASE_URL_SECONDARY", "")
    )
    if not neon_url or not supabase_url:
        print("エラー: DATABASE_URL(Neon)とDATABASE_URL_FALLBACK(Supabase)の両方が必要です。")
        sys.exit(1)
    neon_engine = create_engine(neon_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    supabase_engine = create_engine(supabase_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    return neon_engine, supabase_engine


def sync_table(source_conn, target_conn, table_name: str) -> int:
    """
    source にあって target に無い id の行を追記する。

    旧実装は target の MAX(id) より大きい行だけを取っていたため、
    副系で採番された「MAX以下の欠けた id」が永久に同期されなかった。
    差分があるのに 0 件になる原因はこれ。
    """
    target_ids = {
        r[0] for r in target_conn.execute(text(f"SELECT id FROM {table_name}")).fetchall()
    }
    result = source_conn.execute(text(f"SELECT * FROM {table_name} ORDER BY id"))
    rows = result.mappings().all()
    missing = [row for row in rows if row["id"] not in target_ids]
    if not missing:
        return 0

    columns = list(missing[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    insert_sql = text(
        f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
    )
    for row in missing:
        target_conn.execute(insert_sql, dict(row))

    target_conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
            f"(SELECT COALESCE(MAX(id), 1) FROM {table_name}))"
        )
    )
    return len(missing)



def sync_bankroll_state(source_conn, target_conn) -> str:
    """証拠金残高(1行のみ)は、更新日時が新しい方の内容で上書きする。"""
    source_row = source_conn.execute(
        text("SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1")
    ).mappings().first()
    if source_row is None:
        return "Supabase側にbankroll_stateが無いためスキップしました"

    target_row = target_conn.execute(
        text("SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1")
    ).mappings().first()

    if target_row is not None and target_row["updated_at"] and source_row["updated_at"]:
        if target_row["updated_at"] >= source_row["updated_at"]:
            return "Neon側の証拠金残高の方が新しいため、上書きしませんでした"

    if target_row is None:
        target_conn.execute(
            text(
                "INSERT INTO bankroll_state (id, current_balance, initial_balance, updated_at) "
                "VALUES (1, :current_balance, :initial_balance, :updated_at)"
            ),
            dict(source_row),
        )
    else:
        target_conn.execute(
            text(
                "UPDATE bankroll_state SET current_balance = :current_balance, "
                "initial_balance = :initial_balance, updated_at = :updated_at WHERE id = 1"
            ),
            dict(source_row),
        )
    return f"証拠金残高を更新しました(残高: {source_row['current_balance']}円)"


def main():
    neon_engine, supabase_engine = get_engines()
    print(f"[{datetime.now().isoformat()}] Supabase→Neon 差分同期を開始します")

    with supabase_engine.connect() as supabase_conn, neon_engine.connect() as neon_conn:
        total = 0
        for table_name in TABLES_IN_ORDER:
            try:
                count = sync_table(supabase_conn, neon_conn, table_name)
                neon_conn.commit()
                print(f"  {table_name}: {count}件追記")
                total += count
            except Exception as e:
                neon_conn.rollback()
                print(f"  {table_name}: 同期失敗 - {e}")

        try:
            msg = sync_bankroll_state(supabase_conn, neon_conn)
            neon_conn.commit()
            print(f"  bankroll_state: {msg}")
        except Exception as e:
            neon_conn.rollback()
            print(f"  bankroll_state: 同期失敗 - {e}")

    print(f"[{datetime.now().isoformat()}] 完了。合計{total}件を追記しました。")
    print("この直後にRenderの環境変数 DATABASE_PREFER=primary に切り替えると、")
    print("Neon側で今までと同レベルの補正が効いた状態で予想を継続できます。")


if __name__ == "__main__":
    main()
