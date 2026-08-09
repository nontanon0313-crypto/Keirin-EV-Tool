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
    """テーブルが無ければ作成する。起動時に呼び出す。"""
    if engine is not None:
        Base.metadata.create_all(bind=engine)
