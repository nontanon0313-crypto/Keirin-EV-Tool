"""
管理用: Neon⇔Supabase 差分同期を HTTP から実行する。
Render 無料枠は Shell が使えないため、Termux 等から curl で叩けるようにする。
"""
import os
import subprocess
import sys
from pathlib import Path as PathLib

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/admin", tags=["admin"])


def _project_root() -> PathLib:
    return PathLib(__file__).resolve().parents[2]


def _check_token(token: str) -> None:
    secret = (os.environ.get("ADMIN_SYNC_SECRET") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SYNC_SECRET が未設定です。Render の Environment に追加してください",
        )
    if token != secret:
        raise HTTPException(status_code=403, detail="トークンが違います")


def _require_both_db_urls() -> None:
    neon = (os.environ.get("DATABASE_URL") or "").strip()
    supabase = (
        os.environ.get("DATABASE_URL_FALLBACK")
        or os.environ.get("DATABASE_URL_SECONDARY")
        or ""
    ).strip()
    if not neon or not supabase:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL(Neon) と DATABASE_URL_FALLBACK(Supabase) の両方が必要です",
        )


def _run_sync_script(script_name: str) -> dict:
    root = _project_root()
    script = root / "scraper" / script_name
    if not script.is_file():
        alt = root / script_name
        script = alt if alt.is_file() else script
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"同期スクリプトが見つかりません: {script}")

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=1800,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="同期がタイムアウトしました(30分)")

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={"returncode": proc.returncode, "log": out[-8000:]},
        )
    return {"ok": True, "script": script_name, "log": out[-8000:]}


@router.post("/sync-neon-to-supabase")
def sync_neon_to_supabase(
    token: str = Query(..., description="Render の ADMIN_SYNC_SECRET と同じ値"),
):
    """Neon(主) → Supabase(副) の差分同期。バックアップ・切替前用。"""
    _check_token(token)
    _require_both_db_urls()
    return _run_sync_script("sync_neon_to_supabase.py")


@router.post("/sync-supabase-to-neon")
def sync_supabase_to_neon(
    token: str = Query(..., description="Render の ADMIN_SYNC_SECRET と同じ値"),
):
    """
    Supabase(副) → Neon(主) の差分同期。
    Neon制限中に fallback へ書いた分を、主系復帰後に戻すために使う。
    """
    _check_token(token)
    _require_both_db_urls()
    return _run_sync_script("sync_supabase_to_neon.py")


@router.get("/compare-db-counts")
def compare_db_counts(
    token: str = Query(..., description="Render の ADMIN_SYNC_SECRET と同じ値"),
):
    """
    Neon と Supabase の各テーブル COUNT / MAX(id) を並べて返す。
    差分同期の取りこぼし有無の確認用。
    """
    from sqlalchemy import create_engine, text

    _check_token(token)
    _require_both_db_urls()

    def _norm(url: str) -> str:
        url = (url or "").strip()
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if "channel_binding=" in url:
            base, _, q = url.partition("?")
            qs = "&".join(x for x in q.split("&") if x and not x.startswith("channel_binding"))
            url = base + (("?" + qs) if qs else "")
        return url

    neon_url = _norm(os.environ.get("DATABASE_URL", ""))
    sb_url = _norm(
        os.environ.get("DATABASE_URL_FALLBACK", "")
        or os.environ.get("DATABASE_URL_SECONDARY", "")
    )
    tables = [
        "bank_master",
        "races",
        "entries",
        "odds",
        "ev_results",
        "purchases",
        "skipped_bets",
    ]
    neon_eng = create_engine(neon_url, pool_pre_ping=True, connect_args={"connect_timeout": 15})
    sb_eng = create_engine(sb_url, pool_pre_ping=True, connect_args={"connect_timeout": 15})
    out = {}
    with neon_eng.connect() as nc, sb_eng.connect() as sc:
        for table in tables:
            try:
                n_cnt, n_max = nc.execute(
                    text(f"SELECT COUNT(*), COALESCE(MAX(id),0) FROM {table}")
                ).fetchone()
            except Exception as e:
                n_cnt, n_max = None, str(e)[:120]
            try:
                s_cnt, s_max = sc.execute(
                    text(f"SELECT COUNT(*), COALESCE(MAX(id),0) FROM {table}")
                ).fetchone()
            except Exception as e:
                s_cnt, s_max = None, str(e)[:120]
            gap = None
            if isinstance(n_cnt, int) and isinstance(s_cnt, int):
                gap = s_cnt - n_cnt
            out[table] = {
                "neon_count": n_cnt,
                "neon_max_id": n_max,
                "supabase_count": s_cnt,
                "supabase_max_id": s_max,
                "supabase_minus_neon": gap,
            }
    return {"ok": True, "tables": out}
