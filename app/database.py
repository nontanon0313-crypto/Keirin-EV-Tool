import os
import threading
import logging
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError

logger = logging.getLogger("keirin.database")

# ---------------------------------------------------------------------------
# 接続先
#   DATABASE_URL          … 主系 (現状: Neon など)
#   DATABASE_URL_FALLBACK … 副系 (推奨: Supabase の Postgres 接続文字列)
#   DATABASE_PREFER       … "primary" | "fallback" 起動時の優先 (省略時 primary)
#
# Neon 無料枠の CU-hours 超過などで主系が落ちた場合、副系へ自動切替する。
# 注意: 両DBに同じデータが入っている必要がある(初回は手動マイグレーション)。
# ---------------------------------------------------------------------------

Base = declarative_base()

_lock = threading.RLock()
_active_name = "primary"  # "primary" | "fallback"
_engines = {}  # name -> engine
_sessions = {}  # name -> sessionmaker


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _make_engine(url: str):
    if not url:
        return None
    # pool_pre_ping: 死んだ接続を検出 / connect_timeout で長待ち防止
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"connect_timeout": 10},
    )


PRIMARY_URL = _normalize_url(os.environ.get("DATABASE_URL", ""))
FALLBACK_URL = _normalize_url(
    os.environ.get("DATABASE_URL_FALLBACK", "")
    or os.environ.get("DATABASE_URL_SECONDARY", "")
)
PREFER = (os.environ.get("DATABASE_PREFER", "primary") or "primary").strip().lower()
if PREFER not in ("primary", "fallback"):
    PREFER = "primary"

if PRIMARY_URL:
    _engines["primary"] = _make_engine(PRIMARY_URL)
    _sessions["primary"] = sessionmaker(
        autocommit=False, autoflush=False, bind=_engines["primary"]
    )
if FALLBACK_URL:
    _engines["fallback"] = _make_engine(FALLBACK_URL)
    _sessions["fallback"] = sessionmaker(
        autocommit=False, autoflush=False, bind=_engines["fallback"]
    )

# 起動時の優先先
if PREFER == "fallback" and "fallback" in _engines:
    _active_name = "fallback"
elif "primary" in _engines:
    _active_name = "primary"
elif "fallback" in _engines:
    _active_name = "fallback"
else:
    _active_name = "primary"

# 後方互換: 他モジュールが engine / SessionLocal を参照する場合がある
engine = _engines.get(_active_name)
SessionLocal = _sessions.get(_active_name)


def get_active_db_info() -> dict:
    """どの接続を使っているか(ヘルスチェック用)。"""
    with _lock:
        url = PRIMARY_URL if _active_name == "primary" else FALLBACK_URL
        host = ""
        if url:
            try:
                # postgresql://user:pass@host:port/db
                host = url.split("@", 1)[1].split("/", 1)[0]
            except Exception:
                host = "(unknown)"
        return {
            "active": _active_name,
            "host": host,
            "has_primary": "primary" in _engines,
            "has_fallback": "fallback" in _engines,
            "prefer": PREFER,
        }


def _is_failover_worthy(exc: BaseException) -> bool:
    """Neon制限・接続不能など、副系へ切替えるべきエラーか。"""
    msg = str(exc).lower()
    keywords = (
        "compute time quota",
        "compute quota",
        "quota exceeded",
        "exceeded the compute",
        "remaining compute",
        "free tier limit",
        "limit exceeded",
        "rate limit",
        "too many connections",
        "connection refused",
        "could not connect",
        "connection timed out",
        "timeout expired",
        "server closed the connection",
        "ssl connection has been closed",
        "connection reset",
        "is not accepting connections",
        "the database system is shutting down",
        "terminating connection",
        "no route to host",
        "name or service not known",
        "temporarily unavailable",
        "cannot acquire",
        "neon",  # neon固有メッセージの兜底
    )
    if any(k in msg for k in keywords):
        return True
    # SQLAlchemy の接続系
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return False


def _switch_to(name: str) -> bool:
    """アクティブ接続を name に切替。成功したら True。"""
    global engine, SessionLocal, _active_name
    with _lock:
        if name not in _engines or name not in _sessions:
            return False
        if _active_name == name:
            return True
        old = _active_name
        _active_name = name
        engine = _engines[name]
        SessionLocal = _sessions[name]
        logger.warning("database failover: %s -> %s", old, name)
        return True


def _other_name(name: str) -> Optional[str]:
    if name == "primary" and "fallback" in _engines:
        return "fallback"
    if name == "fallback" and "primary" in _engines:
        return "primary"
    return None


def _ping(session_factory) -> None:
    db = session_factory()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()


def ensure_active_connection() -> None:
    """
    起動時・必要時に、優先先が生きているか確認し、ダメなら副系へ。
    """
    global engine, SessionLocal, _active_name
    order = []
    if PREFER == "fallback":
        order = [n for n in ("fallback", "primary") if n in _engines]
    else:
        order = [n for n in ("primary", "fallback") if n in _engines]
    if not order:
        return

    last_err = None
    for name in order:
        try:
            _ping(_sessions[name])
            _switch_to(name)
            logger.info("database active: %s", name)
            return
        except Exception as e:
            last_err = e
            logger.warning("database probe failed (%s): %s", name, e)
    if last_err:
        logger.error("all database endpoints failed; last error: %s", last_err)


def get_db():
    """
    リクエストごとにセッションを返す。
    接続エラーが「制限・不通」系なら副系へ切替えて1回だけリトライする。
    """
    if not _sessions:
        raise RuntimeError(
            "DATABASE_URL が未設定です。Render の環境変数に "
            "DATABASE_URL (と必要なら DATABASE_URL_FALLBACK) を設定してください。"
        )

    with _lock:
        name = _active_name
        factory = _sessions.get(name)

    db = None
    try:
        db = factory()
        # 軽い生存確認(死んだコネクションを早めに検出)
        db.execute(text("SELECT 1"))
    except Exception as e:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
            db = None
        other = _other_name(name)
        if other and _is_failover_worthy(e):
            logger.warning(
                "database error on %s (%s); trying %s", name, e, other
            )
            if _switch_to(other):
                try:
                    db = _sessions[other]()
                    db.execute(text("SELECT 1"))
                except Exception:
                    if db is not None:
                        try:
                            db.close()
                        except Exception:
                            pass
                    raise
            else:
                raise
        else:
            raise

    try:
        yield db
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def init_db():
    """テーブル作成 + 簡易マイグレーション。アクティブな接続先に対して実行。"""
    ensure_active_connection()
    if engine is None:
        return
    Base.metadata.create_all(bind=engine)

    migrations = [
        "ALTER TABLE ev_results ADD COLUMN IF NOT EXISTS is_recommended BOOLEAN DEFAULT FALSE",
        "ALTER TABLE odds ADD COLUMN IF NOT EXISTS total_vote_amount FLOAT",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS final_odds FLOAT",
        "ALTER TABLE entries ADD COLUMN IF NOT EXISTS is_local BOOLEAN",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS lines_data JSON",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS race_stage VARCHAR(30)",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS weather VARCHAR(20)",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS temperature_c FLOAT",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS season VARCHAR(10)",
        "ALTER TABLE entries ADD COLUMN IF NOT EXISTS pre_race_comment TEXT",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS development_simulation TEXT",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS actual_result VARCHAR(30)",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS external_ref VARCHAR(100)",
        "ALTER TABLE races ADD COLUMN IF NOT EXISTS post_time TIMESTAMP",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_races_external_ref_unique ON races (external_ref) WHERE external_ref IS NOT NULL",
        "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS win_prob_raw FLOAT",
        "ALTER TABLE skipped_bets ADD COLUMN IF NOT EXISTS win_prob_raw FLOAT",
        "ALTER TABLE ev_results ADD COLUMN IF NOT EXISTS estimated_win_prob_raw FLOAT",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    _seed_bank_master()

    # 副系がある場合、スキーマだけ揃えておく(データ移行は手動)
    other = _other_name(_active_name)
    if other and other in _engines:
        try:
            Base.metadata.create_all(bind=_engines[other])
            with _engines[other].connect() as conn:
                for stmt in migrations:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            logger.info("schema ensured on fallback database as well")
        except Exception as e:
            logger.warning("could not prepare fallback schema: %s", e)


def _seed_bank_master():
    """bank_master が空のとき全国43場を投入。"""
    from . import models
    from .keirin_data import get_bank_seed_data

    if SessionLocal is None:
        return
    db = SessionLocal()
    try:
        existing_count = db.query(models.BankMaster).count()
        if existing_count > 0:
            return
        for row in get_bank_seed_data():
            db.add(models.BankMaster(**row))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
