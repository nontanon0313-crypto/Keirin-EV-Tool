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


@router.post("/recommend-race-pct")
def recommend_race_pct(req: schemas.RecommendRacePctRequest):
    """
    許容する破産確率の上限から、それを満たす「1レースあたりの投資上限%」の
    最大値を二分探索で逆算する(のんの要望により追加)。
    1レース上限%が高いほど破産確率も上がる(単調増加)という前提で、
    条件を満たす範囲の中で最大の上限%を探す。
    """
    if req.bets_per_race <= 0 or req.num_races <= 0:
        return {
            "見つかった": False,
            "メッセージ": "1レースの点数とレース数は1以上を指定してください。",
        }

    lo, hi = 0.0, 100.0
    best = None  # (1レース上限%, そのときのシミュレーション結果)
    # 二分探索中は試行を薄く、最後に本計算(レース数が多いと18×5000がタイムアウトするため)
    search_trials = min(800, max(300, req.num_trials // 3))
    for _ in range(12):  # 0.02pt 程度まで十分
        mid = (lo + hi) / 2
        stake_fraction = (mid / 100) / req.bets_per_race
        sim = calc.monte_carlo_bankruptcy(
            initial_bankroll=req.initial_bankroll,
            win_prob=req.win_prob,
            odds_value=req.odds_value,
            stake_fraction=stake_fraction,
            num_bets_per_trial=req.bets_per_race * req.num_races,
            num_trials=search_trials,
            ruin_threshold_pct=req.ruin_threshold_pct,
        )
        if sim["ruin_probability_pct"] <= req.max_ruin_probability_pct:
            best = (mid, sim)
            lo = mid
        else:
            hi = mid

    if best is None:
        return {
            "見つかった": False,
            "メッセージ": (
                f"1レース上限を0%に近づけても、許容破産確率"
                f"{req.max_ruin_probability_pct}%を満たせませんでした。"
                "勝率・オッズの前提を見直してください。"
            ),
        }

    pct, _ = best
    # 採用候補で本計算(表示用に精度を上げる)
    final_sim = calc.monte_carlo_bankruptcy(
        initial_bankroll=req.initial_bankroll,
        win_prob=req.win_prob,
        odds_value=req.odds_value,
        stake_fraction=(pct / 100) / req.bets_per_race,
        num_bets_per_trial=req.bets_per_race * req.num_races,
        num_trials=req.num_trials,
        ruin_threshold_pct=req.ruin_threshold_pct,
    )
    return {
        "見つかった": True,
        "メッセージ": f"1レース上限{pct:.1f}%なら、破産確率を{req.max_ruin_probability_pct}%以下に抑えられます。",
        "推奨_1レース上限%": round(pct, 1),
        "その上限での破産確率%": final_sim["ruin_probability_pct"],
        "黒字化率%": final_sim["profit_probability_pct"],
        "平均最終資金": final_sim["average_final_bankroll"],
        "中央値最終資金": final_sim["median_final_bankroll"],
        "試行回数": final_sim["num_trials"],
    }
