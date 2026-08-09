from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, JSON
)
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class BankMaster(Base):
    """競輪場マスタ：バンク特性を保持"""
    __tablename__ = "bank_master"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False)  # 例: 佐世保
    lap_length_m = Column(Float, nullable=True)  # 周長(m)
    home_stretch_length_m = Column(Float, nullable=True)  # 直線(ホームストレッチ)長(m)
    lead_advantage_score = Column(Float, default=0.5)  # 先行有利度 0(差し有利)〜1(先行絶対有利)
    notes = Column(Text, nullable=True)

    races = relationship("Race", back_populates="bank")


class Race(Base):
    """レース情報"""
    __tablename__ = "races"

    id = Column(Integer, primary_key=True, index=True)
    venue_name = Column(String(50), nullable=False)  # 開催場名（bank_masterと名前で紐付け）
    bank_id = Column(Integer, ForeignKey("bank_master.id"), nullable=True)
    race_number = Column(Integer, nullable=False)  # 例: 4 (4R)
    grade = Column(String(10), nullable=True)  # GI/GII/GIII/F1/F2等
    event_title = Column(String(200), nullable=True)
    race_date = Column(DateTime, nullable=True)
    deadline_time = Column(DateTime, nullable=True)
    source_app = Column(String(50), nullable=True)  # 読み取り元アプリ(分かれば)
    created_at = Column(DateTime, default=datetime.utcnow)

    bank = relationship("BankMaster", back_populates="races")
    entries = relationship("Entry", back_populates="race", cascade="all, delete-orphan")
    odds_list = relationship("Odds", back_populates="race", cascade="all, delete-orphan")
    ev_results = relationship("EvResult", back_populates="race", cascade="all, delete-orphan")


class Entry(Base):
    """出走選手データ"""
    __tablename__ = "entries"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    waku_number = Column(Integer, nullable=True)  # 枠番
    car_number = Column(Integer, nullable=False)  # 車番
    player_name = Column(String(100), nullable=False)
    region = Column(String(50), nullable=True)
    player_class = Column(String(20), nullable=True)  # 級班 例: L1
    age = Column(Integer, nullable=True)
    period = Column(String(20), nullable=True)  # 期別

    # 基本情報
    evaluation_rank = Column(String(20), nullable=True)  # No.1クラウン等
    race_score = Column(Float, nullable=True)  # 競走得点
    leg_style = Column(String(20), nullable=True)  # 脚質: 逃/追込/両/自力等
    s_count = Column(Integer, nullable=True)

    # 直近成績
    h_count = Column(Integer, nullable=True)  # 逃げ回数
    b_count = Column(Integer, nullable=True)  # まくり回数
    kimarite_nige = Column(Integer, nullable=True)
    kimarite_makuri = Column(Integer, nullable=True)
    kimarite_sashi = Column(Integer, nullable=True)
    kimarite_mark = Column(Integer, nullable=True)
    finish_1st = Column(Integer, nullable=True)
    finish_2nd = Column(Integer, nullable=True)
    finish_3rd = Column(Integer, nullable=True)

    # アプリ側勝率(例: tipstar)
    app_win_rate = Column(Float, nullable=True)  # %
    app_2nd_rate = Column(Float, nullable=True)
    app_3rd_rate = Column(Float, nullable=True)
    gear_ratio = Column(Float, nullable=True)

    line_group = Column(String(50), nullable=True)  # ライン構成(例: "1-2" "単騎"等)

    # AI独自推定
    ai_win_prob = Column(Float, nullable=True)  # 0-1
    blended_win_prob = Column(Float, nullable=True)  # 0-1 (app_win_rateとai_win_probの合成)

    race = relationship("Race", back_populates="entries")


class Odds(Base):
    """買い目別オッズ"""
    __tablename__ = "odds"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    bet_type = Column(String(20), nullable=False)  # 単勝/複勝/2車単/2車複/2枠単/2枠複/ワイド/3連単/3連複
    combination = Column(String(20), nullable=False)  # 例: "1-2-3"
    odds_value = Column(Float, nullable=False)
    popularity_rank = Column(Integer, nullable=True)
    total_vote_amount = Column(Float, nullable=True)  # その買い目への投票総額(円)。画面に表示されていれば取得、無ければnull
    updated_at = Column(DateTime, default=datetime.utcnow)

    race = relationship("Race", back_populates="odds_list")


class EvResult(Base):
    """買い目ごとの期待値計算結果"""
    __tablename__ = "ev_results"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    bet_type = Column(String(20), nullable=False)
    combination = Column(String(50), nullable=False)  # 通常の買い目、またはBOX/マルチ裏目の場合は車番集合
    is_box = Column(Boolean, default=False)
    is_multi_ura = Column(Boolean, default=False)

    estimated_win_prob = Column(Float, nullable=False)  # 合成推定確率(Harville式等で算出)
    market_prob = Column(Float, nullable=False)  # オッズから逆算した市場確率
    odds_value = Column(Float, nullable=False)
    ev_pct = Column(Float, nullable=False)  # 期待値% = (推定確率×オッズ - 1) × 100
    kelly_fraction = Column(Float, nullable=True)  # フラクショナルケリー後の推奨賭け比率
    recommended_stake = Column(Float, nullable=True)  # 推奨購入金額(円)
    is_skip = Column(Boolean, default=False)  # 見送り判定
    skip_reason = Column(String(200), nullable=True)
    is_recommended = Column(Boolean, default=False)  # 買い示唆(期待値100円あたり1円以上、かつ見送り対象でない)

    created_at = Column(DateTime, default=datetime.utcnow)

    race = relationship("Race", back_populates="ev_results")


class Purchase(Base):
    """実際の購入履歴・結果"""
    __tablename__ = "purchases"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    ev_result_id = Column(Integer, ForeignKey("ev_results.id"), nullable=True)

    bet_type = Column(String(20), nullable=False)
    combination = Column(String(50), nullable=False)
    stake_amount = Column(Float, nullable=False)
    odds_at_purchase = Column(Float, nullable=True)
    win_prob_at_purchase = Column(Float, nullable=True)
    ev_pct_at_purchase = Column(Float, nullable=True)

    result = Column(String(20), default="pending")  # pending/win/lose
    payout_amount = Column(Float, default=0)

    tags = Column(JSON, nullable=True)  # {"bank":..,"leg_style":..,"line_config":..,"prob_bucket":..}

    purchased_at = Column(DateTime, default=datetime.utcnow)


class BankrollState(Base):
    """
    証拠金の残高を管理する(常に1行のみ)。
    購入記録時に自動で減算し、結果登録時に払戻分を自動で加算する。
    これにより「1レースに全額投票してしまう」ことを構造的に防ぐ。
    """
    __tablename__ = "bankroll_state"

    id = Column(Integer, primary_key=True, default=1)
    current_balance = Column(Float, nullable=False)
    initial_balance = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SkippedBet(Base):
    """見送った買い目の記録(後から検証するため)"""
    __tablename__ = "skipped_bets"

    id = Column(Integer, primary_key=True, index=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    bet_type = Column(String(20), nullable=False)
    combination = Column(String(50), nullable=False)
    win_prob_estimated = Column(Float, nullable=True)
    ev_pct_estimated = Column(Float, nullable=True)
    reason = Column(String(200), nullable=True)  # 例: "期待値マイナス" "最低勝率フィルター抵触"

    actual_result = Column(String(20), nullable=True)  # 後で結果が分かれば埋める(win/lose)
    actual_payout = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
