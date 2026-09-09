from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class BankMasterCreate(BaseModel):
    name: str
    lap_length_m: Optional[float] = None
    home_stretch_length_m: Optional[float] = None
    lead_advantage_score: Optional[float] = 0.5
    notes: Optional[str] = None


class BankMasterOut(BankMasterCreate):
    id: int

    class Config:
        from_attributes = True


class PurchaseCreate(BaseModel):
    race_id: int
    ev_result_id: Optional[int] = None
    bet_type: str
    combination: str
    stake_amount: float
    odds_at_purchase: Optional[float] = None
    win_prob_at_purchase: Optional[float] = None
    win_prob_raw: Optional[float] = None
    ev_pct_at_purchase: Optional[float] = None
    tags: Optional[dict] = None


class PurchaseBulkCreate(BaseModel):
    items: List[PurchaseCreate]


class PurchaseResultUpdate(BaseModel):
    result: str  # win / lose
    payout_amount: float = 0
    final_odds: Optional[float] = None  # 締切時の最終オッズ(分かれば)


class SimulationRequest(BaseModel):
    initial_bankroll: float
    win_prob: float
    odds_value: float
    stake_fraction: float
    num_bets_per_trial: int = 100
    num_trials: int = 5000
    ruin_threshold_pct: float = 0.5


class RecommendRacePctRequest(BaseModel):
    initial_bankroll: float
    win_prob: float
    odds_value: float
    bets_per_race: int
    num_races: int
    max_ruin_probability_pct: float
    num_trials: int = 2000
    ruin_threshold_pct: float = 0.5
    bet_type: Optional[str] = None  # 指定すると実績ブートストラップをその券種に限定する


class BootstrapSimulationRequest(BaseModel):
    initial_bankroll: float
    stake_fraction: float
    num_bets_per_trial: int = 100
    num_trials: int = 5000
    ruin_threshold_pct: float = 0.5
    bet_type: Optional[str] = None  # 指定すると実績データをその券種に限定する


class EvCalcRequest(BaseModel):
    apply_calibration: bool = True
    apply_purchase_set_calibration: bool = True  # 購入集合の残差校正（第2段）
    race_id: int
    bankroll: Optional[float] = None  # 未指定なら証拠金残高を自動使用
    fractional_coefficient: float = 0.25  # 固定値として運用(通常は変更不要)
    max_bet_pct_per_bet: float = 0.05
    min_win_prob: float = 0.05
    min_ev_pct: float = 50.0  # 実績改善を優先した最低EVライン
    rebate_pct: float = 0.0  # 還元レース(勝敗に関わらずポイント還元)の場合、還元率(0-1)を指定


class RaceCapSet(BaseModel):
    race_cap_pct: float  # 0.10 = 10%。資金管理シミュレーションで確認した安全な値を設定する


class RacePlanRequest(BaseModel):
    race_id: int
    bankroll: Optional[float] = None  # 未指定なら証拠金残高を自動使用
    fractional_coefficient: float = 0.25  # 固定値として運用(通常は変更不要)
    max_bet_pct_per_bet: float = 0.05  # 1点あたりの上限比率
    max_race_pct: Optional[float] = None  # 未指定なら資金管理シミュレーションで確認した値(bankroll_state.race_cap_pct)を自動使用
    min_win_prob: float = 0.005
    min_ev_pct: float = 50.0
    rebate_pct: float = 0.0  # 還元レース(勝敗に関わらずポイント還元)の場合、還元率(0-1)を指定
    max_items: int = 20  # 投票アプリへの手入力を現実的な時間で終えられる件数の上限
    exclude_low_prob_warning: bool = False  # 大穴帯(0-5%・実績未検証)も候補として評価し、警告は表示する
    apply_calibration: bool = True  # 勝率帯キャリブレーションを適用するか(検証用にOFF可)
    apply_purchase_set_calibration: bool = True  # 購入集合の残差校正（第2段）
    avoid_garami: bool = True  # 券種をまたいで「的中したのに合計投票額を下回る(ガミる)」結果が起きないよう選定するか
    apply_performance_gates: bool = True  # 不調ステージ見送りのみ(券種・帯の除外はしない。設計厳守)
    apply_odds_safety_margin: bool = True  # 過去のPurchase実績から算出したオッズ安全マージンを適用するか
    max_single_bet_pct_of_race_cap: float = 0.4  # 1点の投票額がレース予算(race_cap)に占める上限比率。
    # 候補が1〜2点しかない時に予算全額を1点に集中させないための歯止め(既定40%)。


class BankrollSet(BaseModel):
    initial_balance: float


# --- 収益タブ(実資金・Purchaseとは完全分離) ---

class LiveBetPlanItem(BaseModel):
    bet_type: str
    combination: str
    planned_stake: Optional[float] = None
    planned_win_prob: Optional[float] = None  # 0-1
    planned_odds: Optional[float] = None
    planned_ev_pct: Optional[float] = None
    planned_expected_profit: Optional[float] = None


class LiveBetFromPlanCreate(BaseModel):
    race_id: int
    items: List[LiveBetPlanItem]
    # True(既定): プラン通り投票前提で実額=予定額・voted登録
    mark_as_voted: bool = True


class LiveBetWinUpdate(BaseModel):
    """的中時だけ払戻を書く。実額未設定なら planned_stake を採用。"""
    actual_payout: float
    actual_stake: Optional[float] = None


class LiveBetManualCreate(BaseModel):
    race_id: Optional[int] = None
    race_date: Optional[datetime] = None
    venue_name: Optional[str] = None
    race_number: Optional[int] = None
    bet_type: str
    combination: str
    actual_stake: float
    planned_stake: Optional[float] = None
    planned_win_prob: Optional[float] = None
    planned_odds: Optional[float] = None
    planned_ev_pct: Optional[float] = None
    vote_status: str = "voted"  # voted / not_voted
    actual_result: str = "pending"  # pending / win / lose / not_voted
    actual_payout: float = 0.0
    memo: Optional[str] = None


class LiveBetUpdate(BaseModel):
    vote_status: Optional[str] = None
    actual_stake: Optional[float] = None
    actual_result: Optional[str] = None
    actual_payout: Optional[float] = None
    memo: Optional[str] = None


class RevenueSettingsSet(BaseModel):
    pass
