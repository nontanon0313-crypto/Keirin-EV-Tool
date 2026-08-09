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
    ev_pct_at_purchase: Optional[float] = None
    tags: Optional[dict] = None


class PurchaseResultUpdate(BaseModel):
    result: str  # win / lose
    payout_amount: float = 0


class SimulationRequest(BaseModel):
    initial_bankroll: float
    win_prob: float
    odds_value: float
    stake_fraction: float
    num_bets_per_trial: int = 100
    num_trials: int = 5000
    ruin_threshold_pct: float = 0.5


class EvCalcRequest(BaseModel):
    race_id: int
    bankroll: float
    fractional_coefficient: float = 0.25
    max_bet_pct_per_bet: float = 0.05
    min_win_prob: float = 0.05
