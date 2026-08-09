from fastapi import APIRouter
from .. import schemas
from .. import ev_calculator as calc

router = APIRouter(prefix="/simulation", tags=["simulation"])


@router.post("/bankruptcy")
def bankruptcy_simulation(req: schemas.SimulationRequest):
    """
    現在の運用ルール(勝率・オッズ・賭け比率)を続けた場合の破産確率をシミュレーションする。
    """
    result = calc.monte_carlo_bankruptcy(
        initial_bankroll=req.initial_bankroll,
        win_prob=req.win_prob,
        odds_value=req.odds_value,
        stake_fraction=req.stake_fraction,
        num_bets_per_trial=req.num_bets_per_trial,
        num_trials=req.num_trials,
        ruin_threshold_pct=req.ruin_threshold_pct,
    )
    return result
