import os
import threading
import logging
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError

logger = logging.getLogger("keirin.database")

# DATABASE_URL           = 主系 (Neon など)
# DATABASE_URL_FALLBACK  = 副系 (Supabase など)  ← 主系制限時に自動使用
# DATABASE_URL_FALLBACK2 = 第3系 (Aiven など)     ← 主系・副系ともに制限時に自動使用
# DATABASE_PREFER        = primary | fallback | fallback2

Base = declarative_base()

_lock = threading.RLock()
_active_name = "primary"
_engines = {}
_sessions = {}

_TIER_ORDER = ("primary", "fallback", "fallback2")
_TIER_ENV_VARS = {
    "primary": ("DATABASE_URL",),
    "fallback": ("DATABASE_URL_FALLBACK", "DATABASE_URL_SECONDARY"),
    "fallback2": ("DATABASE_URL_FALLBACK2", "DATABASE_URL_TERTIARY"),
}


def _normalize_url(url: str) -> str:
    """接続URLを正規化する。channel_binding=require は psycopg2 で失敗しやすいため外す。"""
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # SQLAlchemy+psycopg2 では channel_binding が原因で primary に繋がらない事例がある
    if "channel_binding=" in url:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        try:
            parts = urlparse(url)
            q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "channel_binding"]
            url = urlunparse(parts._replace(query=urlencode(q)))
        except Exception:
            url = url.replace("&channel_binding=require", "").replace("?channel_binding=require&", "?").replace("?channel_binding=require", "")
    return url


def _make_engine(url: str):
    if not url:
        return None
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "connect_timeout": 10,
            # 2026-09-12: connect_timeoutはTCP接続確立までしかカバーせず、
            # Aivenが休止から復帰する途中など「接続は通るがクエリ応答が遅れる」
            # ケースでSELECT 1自体がハングし続ける可能性があった。
            # statement_timeoutでクエリ単位でも確実にタイムアウトさせる。
            "options": "-c statement_timeout=8000",
        },
    )


_TIER_URLS = {}
for _name in _TIER_ORDER:
    _url = ""
    for _env_key in _TIER_ENV_VARS[_name]:
        _url = os.environ.get(_env_key, "")
        if _url:
            break
    _TIER_URLS[_name] = _normalize_url(_url)

PRIMARY_URL = _TIER_URLS["primary"]
FALLBACK_URL = _TIER_URLS["fallback"]
FALLBACK2_URL = _TIER_URLS["fallback2"]

PREFER = (os.environ.get("DATABASE_PREFER", "primary") or "primary").strip().lower()
if PREFER not in _TIER_ORDER:
    PREFER = "primary"

for _name in _TIER_ORDER:
    _url = _TIER_URLS[_name]
    if _url:
        _engines[_name] = _make_engine(_url)
        _sessions[_name] = sessionmaker(autocommit=False, autoflush=False, bind=_engines[_name])

_default_order = [n for n in _TIER_ORDER if n in _engines]
if PREFER in _default_order:
    _default_order = [PREFER] + [n for n in _default_order if n != PREFER]
_active_name = _default_order[0] if _default_order else "primary"

engine = _engines.get(_active_name)
SessionLocal = _sessions.get(_active_name)


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        return url.split("@", 1)[1].split("/", 1)[0]
    except Exception:
        return "(unknown)"


def get_active_db_info() -> dict:
    """稼働中DBの情報。各系が失敗している場合は対応する *_error に理由を入れる。"""
    with _lock:
        active = _active_name
        prefer = PREFER
    errors = {}
    ok = {}
    for name in _TIER_ORDER:
        if name in _sessions:
            try:
                _ping(_sessions[name])
                ok[name] = True
                errors[name] = None
            except Exception as e:
                ok[name] = False
                errors[name] = str(e)[:500]
    active_url = _TIER_URLS.get(active, "")
    return {
        "active": active,
        "host": _host_of(active_url),
        "primary_host": _host_of(PRIMARY_URL),
        "fallback_host": _host_of(FALLBACK_URL),
        "fallback2_host": _host_of(FALLBACK2_URL),
        "has_primary": "primary" in _engines,
        "has_fallback": "fallback" in _engines,
        "has_fallback2": "fallback2" in _engines,
        "prefer": prefer,
        "primary_ok": ok.get("primary", False),
        "fallback_ok": ok.get("fallback", False),
        "fallback2_ok": ok.get("fallback2", False),
        "primary_error": errors.get("primary"),
        "fallback_error": errors.get("fallback"),
        "fallback2_error": errors.get("fallback2"),
    }


def _is_failover_worthy(exc: BaseException) -> bool:
    msg = str(exc).lower()
    keywords = (
        "data transfer quota",
        "compute time quota",
        "compute quota",
        "quota exceeded",
        "exceeded the compute",
        "exceeded the data transfer",
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
        "upgrade your plan",
    )
    if any(k in msg for k in keywords):
        return True
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return False


def _switch_to(name: str) -> bool:
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


def _other_names(name: str):
    """設定済みの系統のうち、name以外の全てを返す(スキーマ同期用)。"""
    return [n for n in _TIER_ORDER if n in _engines and n != name]


def _ping(session_factory) -> None:
    db = session_factory()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()



def _preferred_order():
    """DATABASE_PREFER に従った接続試行順(primary/fallback/fallback2の中で設定済みのもの)。"""
    available = [n for n in _TIER_ORDER if n in _engines]
    if PREFER in available:
        return [PREFER] + [n for n in available if n != PREFER]
    return available


def ensure_active_connection() -> None:
    """prefer 順で生きているDBを選び _active_name を更新する。"""
    global engine, SessionLocal, _active_name
    order = _preferred_order()
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
        raise last_err


def get_db():
    """
    リクエストごとに prefer 順で接続を試す。
    以前は一度 fallback に落ちるとプロセス終了まで primary に戻らなかった。
    Neon が一時停止→復帰したあとも prefer=primary なら primary を再試行する。
    """
    if not _sessions:
        raise RuntimeError(
            "DATABASE_URL が未設定です。Render に DATABASE_URL "
            "(と必要なら DATABASE_URL_FALLBACK) を設定してください。"
        )

    order = _preferred_order()
    if not order:
        raise RuntimeError("利用可能な DATABASE_URL がありません")

    db = None
    last_err = None
    for name in order:
        try:
            factory = _sessions[name]
            candidate = factory()
            candidate.execute(text("SELECT 1"))
            _switch_to(name)
            db = candidate
            break
        except Exception as e:
            last_err = e
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
                db = None
            # prefer 先頭が失敗した場合のみ次へ。ログは警告に留める
            logger.warning("database probe failed (%s): %s", name, e)
            continue

    if db is None:
        if last_err is not None:
            raise last_err
        raise RuntimeError("database connection failed")

    try:
        yield db
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def init_db():
    """起動時: 生きている方のDBでテーブル作成。主系が死んでいれば副系へ。"""
    try:
        ensure_active_connection()
    except Exception as e:
        logger.error("init_db: no usable database: %s", e)
        raise

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
        "ALTER TABLE bankroll_state ADD COLUMN IF NOT EXISTS race_cap_pct FLOAT DEFAULT 0.10",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    _seed_bank_master()

    for _other in _other_names(_active_name):
        try:
            Base.metadata.create_all(bind=_engines[_other])
            with _engines[_other].connect() as conn:
                for stmt in migrations:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            logger.info("schema ensured on %s database as well", _other)
        except Exception as e:
            logger.warning("could not prepare %s schema: %s", _other, e)


def _seed_bank_master():
    from . import models
    from .keirin_data import get_bank_seed_data

    if SessionLocal is None:
        return
    db = SessionLocal()
    try:
        if db.query(models.BankMaster).count() > 0:
            return
        for row in get_bank_seed_data():
            db.add(models.BankMaster(**row))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
