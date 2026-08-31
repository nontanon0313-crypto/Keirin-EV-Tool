#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Neon(主系)→Supabase(副系)への差分同期スクリプト。

背景:
Neonの無料枠が制限に近づいた通知が来た時、DATABASE_PREFER=fallback に切り替えて
Supabaseで運用を継続できるようにしている(app/database.py参照)。しかしこの
切り替え自体はデータを同期しないため、Supabase側にはキャリブレーション(補正)
に必要な過去の実績データが無く、切り替え直後は無補正モードになってしまう。

このスクリプトは、Neonの制限が近いという通知が来た「切り替え直前」に手動実行し、
Neon側にしかない新規行(前回同期以降に増えた分)だけをSupabaseへ追記する。
毎回全件コピーするとNeonの通信量(データ転送量)を消費してしまい本末転倒なので、
テーブルごとに「Supabase側の最大id」より大きいidの行だけをNeonから取得して
追記する(差分同期)。

対象テーブルはid(自動採番の整数主キー)を持つもの全てで、外部キーの依存関係の
順番(races→entries/odds/ev_results→purchases/skipped_bets)で処理する。
bankroll_state(証拠金残高)は1行しかない特殊なテーブルなので、idベースではなく
updated_atを比較して新しい方の内容で上書きする。

使い方:
    python scraper/sync_neon_to_supabase.py
    (DATABASE_URL=Neon, DATABASE_URL_FALLBACK=Supabase を環境変数から読む。
     .envに設定済みならそのまま実行するだけでよい)

注意:
- 明示的にidを指定してINSERTするため、同期後はSupabase側のシーケンス(次に
  自動採番される値)がズレる。そのままだとSupabase運用中にアプリが新規作成した
  行のidが、後で同期しようとしたNeon側の行のidと衝突する恐れがあるため、
  同期の最後に各テーブルのシーケンスをMAX(id)に合わせ直している。
- 実行は必ずNeon(主系)がまだ生きている(制限に達する前)うちに行うこと。
  Neonが完全に読み込み不能な状態になってからでは、このスクリプト自体が
  Neonからデータを取得できない。
"""
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

# 依存関係の順番(この順でコピーしないと外部キー制約に引っかかる)
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


def sync_table(neon_conn, supabase_conn, table_name: str) -> int:
    """idベースの差分同期。Supabase側の最大idより大きい行だけNeonから取ってきて追記する。"""
    max_id_row = supabase_conn.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table_name}")).fetchone()
    max_id = max_id_row[0] if max_id_row else 0

    result = neon_conn.execute(
        text(f"SELECT * FROM {table_name} WHERE id > :max_id ORDER BY id"), {"max_id": max_id}
    )
    rows = result.mappings().all()
    if not rows:
        return 0

    columns = list(rows[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    insert_sql = text(
        f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
    )
    for row in rows:
        supabase_conn.execute(insert_sql, dict(row))

    # 明示的にidを指定してINSERTしたので、Supabase側の自動採番シーケンスを
    # MAX(id)に合わせ直す(ズレたままだと今後Supabase運用中の新規行が
    # 既存idと衝突する恐れがあるため)
    supabase_conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
            f"(SELECT COALESCE(MAX(id), 1) FROM {table_name}))"
        )
    )
    return len(rows)


def sync_bankroll_state(neon_conn, supabase_conn) -> str:
    """証拠金残高(1行のみ)は、更新日時が新しい方の内容で上書きする。"""
    neon_row = neon_conn.execute(
        text("SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1")
    ).mappings().first()
    if neon_row is None:
        return "Neon側にbankroll_stateが無いためスキップしました"

    supabase_row = supabase_conn.execute(
        text("SELECT current_balance, initial_balance, updated_at FROM bankroll_state WHERE id = 1")
    ).mappings().first()

    if supabase_row is not None and supabase_row["updated_at"] and neon_row["updated_at"]:
        if supabase_row["updated_at"] >= neon_row["updated_at"]:
            return "Supabase側の証拠金残高の方が新しいため、上書きしませんでした"

    if supabase_row is None:
        supabase_conn.execute(
            text(
                "INSERT INTO bankroll_state (id, current_balance, initial_balance, updated_at) "
                "VALUES (1, :current_balance, :initial_balance, :updated_at)"
            ),
            dict(neon_row),
        )
    else:
        supabase_conn.execute(
            text(
                "UPDATE bankroll_state SET current_balance = :current_balance, "
                "initial_balance = :initial_balance, updated_at = :updated_at WHERE id = 1"
            ),
            dict(neon_row),
        )
    return f"証拠金残高を更新しました(残高: {neon_row['current_balance']}円)"


def main():
    neon_engine, supabase_engine = get_engines()
    print(f"[{datetime.now().isoformat()}] Neon→Supabase 差分同期を開始します")

    with neon_engine.connect() as neon_conn, supabase_engine.connect() as supabase_conn:
        total = 0
        for table_name in TABLES_IN_ORDER:
            try:
                count = sync_table(neon_conn, supabase_conn, table_name)
                supabase_conn.commit()
                print(f"  {table_name}: {count}件追記")
                total += count
            except Exception as e:
                supabase_conn.rollback()
                print(f"  {table_name}: 同期失敗 - {e}")

        try:
            msg = sync_bankroll_state(neon_conn, supabase_conn)
            supabase_conn.commit()
            print(f"  bankroll_state: {msg}")
        except Exception as e:
            supabase_conn.rollback()
            print(f"  bankroll_state: 同期失敗 - {e}")

    print(f"[{datetime.now().isoformat()}] 完了。合計{total}件を追記しました。")
    print("この直後にRenderの環境変数 DATABASE_PREFER=fallback に切り替えると、")
    print("Supabase側で今までと同レベルの補正が効いた状態で予想を継続できます。")


if __name__ == "__main__":
    main()
