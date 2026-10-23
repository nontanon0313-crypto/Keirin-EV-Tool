"""
新設計の検証側テーブル定義（7テーブルのうち競輪場マスタ以外の6テーブル）

競輪場マスタ（bank_master）は既存 `app/models.py` の定義を流用する。
既存 `app/models.py` は触らない。実運用側（live_bets / bankroll_state）に影響ゼロ。

設計仕様: docs/新設計_テーブル定義.md
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, JSON, ForeignKey, Index
)
from .database import Base


class RaceResult(Base):
    """1. レース結果（race_results）"""
    __tablename__ = "race_results"

    race_id          = Column(String(100), primary_key=True)
    actual_result    = Column(String(30), nullable=False, comment="実際の着順 例 1-3-2")
    payout_odds_3t   = Column(Float, nullable=True, comment="3連単払戻オッズ")
    payout_amount_3t = Column(Float, nullable=True, comment="3連単払戻金額 100円あたり")
    recorded_at      = Column(DateTime, default=datetime.utcnow, nullable=False)


class RaceInfo(Base):
    """2. レース情報（race_info）"""
    __tablename__ = "race_info"

    race_id     = Column(String(100), primary_key=True)
    venue_name  = Column(String(50), nullable=False, comment="開催場名")
    race_number = Column(Integer, nullable=False, comment="レース番号")
    race_date   = Column(DateTime, nullable=False, comment="開催日")
    post_time   = Column(DateTime, nullable=True, comment="発走時刻 JST")
    grade       = Column(String(10), nullable=True, comment="グレード")
    entries     = Column(JSON, nullable=True, comment="出走表 車番・選手名・直近成績・脚質・ライン")
    details     = Column(JSON, nullable=True, comment="詳細情報 天候・バンク特性・展開予想等")
    fetched_at  = Column(DateTime, default=datetime.utcnow, nullable=False)


class RaceOdds(Base):
    """2b. レースオッズ（race_odds）"""
    __tablename__ = "race_odds"

    race_id           = Column(String(100), primary_key=True)
    bet_type          = Column(String(20), primary_key=True, comment="券種")
    combination       = Column(String(20), primary_key=True, comment="買い目 例 1-2-3")
    odds_value        = Column(Float, nullable=False, comment="オッズ")
    popularity_rank   = Column(Integer, nullable=True, comment="人気順位")
    total_vote_amount = Column(Float, nullable=True, comment="投票総額")
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Assumption(Base):
    """4. 想定（assumptions）— 全買い目の予測マスタ。投票有無も持つ"""
    __tablename__ = "assumptions"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    race_id                = Column(String(100), nullable=False, comment="レースID")
    bet_type               = Column(String(20), nullable=False, comment="券種")
    combination            = Column(String(50), nullable=False, comment="買い目")
    model_version          = Column(String(50), nullable=False, comment="予測モデルバージョン")
    predicted_win_prob     = Column(Float, nullable=False, comment="予想的中率 補正後 0-1")
    predicted_win_prob_raw = Column(Float, nullable=True, comment="予想的中率 補正前")
    market_prob            = Column(Float, nullable=True, comment="市場確率")
    predicted_ev           = Column(Float, nullable=False, comment="想定EV %")
    odds_at_prediction     = Column(Float, nullable=False, comment="予測時オッズ")
    final_odds             = Column(Float, nullable=True, comment="締切時オッズ")
    kelly_fraction         = Column(Float, nullable=True, comment="ケリー比率")
    recommended_stake      = Column(Float, nullable=True, comment="推奨購入額 円")
    voted                  = Column(Integer, nullable=False, default=0, comment="0=見送り 1=投票")
    stake_amount           = Column(Float, nullable=False, default=0, comment="投資額")
    purchased_at           = Column(DateTime, nullable=True, comment="投票日時")
    is_skip                = Column(Boolean, default=False, comment="見送り判定")
    skip_reason            = Column(Text, nullable=True, comment="見送り理由")
    is_recommended         = Column(Boolean, default=False, comment="買い示唆")
    is_box                 = Column(Boolean, default=False, comment="BOX")
    is_multi_ura           = Column(Boolean, default=False, comment="マルチ裏目")
    tags                   = Column(JSON, nullable=True, comment="会場・脚質・ライン構成・確率帯")
    created_at             = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("uq_assumptions_race_bet_combo_model",
              "race_id", "bet_type", "combination", "model_version", unique=True),
        Index("ix_assumptions_race_id", "race_id"),
        Index("ix_assumptions_model_version", "model_version"),
    )


class Verification(Base):
    """5. 検証（verification）— 想定とレース結果を突き合わせた評価値"""
    __tablename__ = "verification"

    assumption_id      = Column(Integer, ForeignKey("assumptions.id"), primary_key=True)
    race_id            = Column(String(100), nullable=False)
    bet_type           = Column(String(20), nullable=False)
    combination        = Column(String(50), nullable=False)
    model_version      = Column(String(50), nullable=False)
    predicted_win_prob = Column(Float, nullable=False, comment="予想的中率")
    predicted_ev       = Column(Float, nullable=False, comment="想定EV")
    actual_odds        = Column(Float, nullable=False, comment="実際のオッズ")
    voted              = Column(Integer, nullable=False, comment="投票有無")
    hit                = Column(Integer, nullable=False, default=0, comment="1=的中")
    stake_amount       = Column(Float, nullable=False, default=0, comment="投資額")
    payout_amount      = Column(Float, nullable=False, default=0, comment="払戻額")
    profit             = Column(Float, nullable=False, default=0, comment="損益")
    prob_band          = Column(String(30), nullable=False, comment="予想的中率帯")
    odds_band          = Column(String(30), nullable=False, comment="オッズ帯")
    ev_band            = Column(String(30), nullable=False, comment="EV帯")
    race_rank_by_ev    = Column(Integer, nullable=False, comment="レース内EV順位 1=最高")
    computed_at        = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_verification_race_id", "race_id"),
        Index("ix_verification_model_version", "model_version"),
        Index("ix_verification_bands", "prob_band", "odds_band", "ev_band"),
    )


class AggregationCache(Base):
    """3. 集計キャッシュ（aggregation_cache）— 画面表示用の集計済み値"""
    __tablename__ = "aggregation_cache"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    scope       = Column(String(200), nullable=False, comment="集計範囲")
    section     = Column(String(50), nullable=False, comment="セクション")
    params      = Column(Text, nullable=False, comment="パラメータ JSON")
    result      = Column(Text, nullable=False, comment="集計結果 JSON")
    source_hash = Column(String(64), nullable=False, comment="入力ハッシュ")
    computed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("uq_aggregation_cache_key", "scope", "section", "params", unique=True),
    )
