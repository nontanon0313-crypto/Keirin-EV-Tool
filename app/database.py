import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# NeonのDATABASE_URLは環境変数で設定する（Renderのダッシュボードで設定）
# 例: postgresql://user:pass@ep-xxxx.neon.tech/dbname?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL.startswith("postgres://"):
    # RenderやNeonがpostgres://で払い出す場合があるためSQLAlchemy用に変換
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """テーブルが無ければ作成する。起動時に呼び出す。
    また、後から追加したカラムを既存DBにも反映する簡易マイグレーションを実行する
    (本格的なAlembic等は導入せず、ADD COLUMN IF NOT EXISTSで都度対応する運用)。
    """
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
    ]
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    _seed_bank_master()


def _seed_bank_master():
    """
    bank_masterテーブルが空の場合のみ、全国43場のマスタデータを自動投入する。
    既にデータがある場合は上書きしない(手動で調整した値を保護するため)。
    """
    from . import models
    from .keirin_data import get_bank_seed_data

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
