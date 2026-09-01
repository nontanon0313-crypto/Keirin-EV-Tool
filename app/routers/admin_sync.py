"""
管理用: Neon→Supabase 差分同期を HTTP から実行する。
Render 無料枠は Shell が使えないため、Termux 等から curl で叩けるようにする。
"""
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/admin", tags=["admin"])


def _project_root() -> Path:
    # app/routers/admin_sync.py → リポジトリルート
    return Path(__file__).resolve().parents[2]


@router.post("/sync-neon-to-supabase")
def sync_neon_to_supabase(
    token: str = Query(..., description="Render の ADMIN_SYNC_SECRET と同じ値"),
):
    """
    Neon(DATABASE_URL) → Supabase(DATABASE_URL_FALLBACK) の差分同期。
    秘密トークン必須。実行はデプロイ先(Render)上で行うため Termux に psycopg2 は不要。
    """
    secret = (os.environ.get("ADMIN_SYNC_SECRET") or "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SYNC_SECRET が未設定です。Render の Environment に追加してください",
        )
    if token != secret:
        raise HTTPException(status_code=403, detail="トークンが違います")

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

    root = _project_root()
    script = root / "scraper" / "sync_neon_to_supabase.py"
    if not script.is_file():
        # Render の配置差異向け
        alt = root / "sync_neon_to_supabase.py"
        script = alt if alt.is_file() else script
    if not script.is_file():
        raise HTTPException(status_code=500, detail=f"同期スクリプトが見つかりません: {script}")

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=600,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="同期がタイムアウトしました(10分)")

    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={"returncode": proc.returncode, "log": out[-8000:]},
        )
    return {"ok": True, "log": out[-8000:]}
