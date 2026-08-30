from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .. import schemas, models
from ..database import get_db
from .. import ev_calculator as calc

router = APIRouter(prefix="/simulation", tags=["simulation"])


def _get_outcome_multipliers(db: Session, bet_type: str = None):
    """
    実際の購入履歴から、1件ごとの payout/stake(外れは0.0)のリストを作る。
    「平均勝率」と「平均オッズ」を別々の数字として組み合わせるのではなく、
    実際に起きた「勝ち負けとその時の倍率」のペアをそのまま使うためのもの
    (のんの実機運用で発覚した、シミュレーション結果が非現実的に膨らむ問題への対応)。
    """
    q = db.query(models.Purchase).filter(
        models.Purchase.result != "pending",
        models.Purchase.stake_amount > 0,
    )
    if bet_type:
        q = q.filter(models.Purchase.bet_type == bet_type)
    purchases = q.all()
    outcomes = []
    for p in purchases:
        if p.result == "win":
            outcomes.append(p.payout_amount / p.stake_amount)
        else:
            outcomes.append(0.0)
    return outcomes


@router.post("/bankruptcy")
def bankruptcy_simulation(req: schemas.SimulationRequest):
    """
    現在の運用ルール(勝率・オッズ・賭け比率)を続けた場合の破産確率をシミュレーションする。

    注意: 勝率とオッズを別々の「平均値」として渡すこの方式は、大穴(低勝率・高オッズ)と
    本命(高勝率・低オッズ)が混在する実際の券種構成を正しく再現できず、結果が非現実的に
    膨らむことがある(のんの実機運用で発覚)。実績データがあるなら
    /simulation/bankruptcy-bootstrap の方が信頼できる。
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


@router.post("/bankruptcy-bootstrap")
def bankruptcy_simulation_bootstrap(req: schemas.BootstrapSimulationRequest, db: Session = Depends(get_db)):
    """
    「平均勝率」と「平均オッズ」を組み合わせるのではなく、実際の購入履歴
    (1件ごとの勝ち負けと倍率のペア)からランダムに抽出してシミュレーションする、
    より実態に即した破産確率シミュレーション(のんの指摘により追加)。
    """
    outcomes = _get_outcome_multipliers(db, bet_type=req.bet_type)
    if len(outcomes) < 20:
        raise HTTPException(
            400,
            f"実績データが少なすぎます(該当{len(outcomes)}件)。20件以上の確定済み購入が必要です。",
        )
    result = calc.monte_carlo_bankruptcy_bootstrap(
        initial_bankroll=req.initial_bankroll,
        outcome_multipliers=outcomes,
        stake_fraction=req.stake_fraction,
        num_bets_per_trial=req.num_bets_per_trial,
        num_trials=req.num_trials,
        ruin_threshold_pct=req.ruin_threshold_pct,
    )
    return result


@router.post("/recommend-race-pct")
def recommend_race_pct(req: schemas.RecommendRacePctRequest, db: Session = Depends(get_db)):
    """
    許容する破産確率の上限から、それを満たす「1レースあたりの投資上限%」の
    最大値を二分探索で逆算する(のんの要望により追加)。
    1レース上限%が高いほど破産確率も上がる(単調増加)という前提で、
    条件を満たす範囲の中で最大の上限%を探す。

    実績データが20件以上あればブートストラップ方式(実際の勝ち負け・倍率のペアを
    使う、より現実に即した方式)を優先して使う。無ければ従来の
    平均勝率×平均オッズ方式にフォールバックする。
    """
    if req.bets_per_race <= 0 or req.num_races <= 0:
        return {
            "見つかった": False,
            "メッセージ": "1レースの点数とレース数は1以上を指定してください。",
        }

    outcomes = _get_outcome_multipliers(db, bet_type=req.bet_type)
    use_bootstrap = len(outcomes) >= 20

    lo, hi = 0.0, 100.0
    best = None  # (1レース上限%, そのときのシミュレーション結果)
    for _ in range(18):  # 100 / 2^18 ≈ 0.0004pt まで絞り込める
        mid = (lo + hi) / 2
        stake_fraction = (mid / 100) / req.bets_per_race
        if use_bootstrap:
            sim = calc.monte_carlo_bankruptcy_bootstrap(
                initial_bankroll=req.initial_bankroll,
                outcome_multipliers=outcomes,
                stake_fraction=stake_fraction,
                num_bets_per_trial=req.bets_per_race * req.num_races,
                num_trials=req.num_trials,
                ruin_threshold_pct=req.ruin_threshold_pct,
            )
        else:
            sim = calc.monte_carlo_bankruptcy(
                initial_bankroll=req.initial_bankroll,
                win_prob=req.win_prob,
                odds_value=req.odds_value,
                stake_fraction=stake_fraction,
                num_bets_per_trial=req.bets_per_race * req.num_races,
                num_trials=req.num_trials,
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

    pct, sim = best
    return {
        "見つかった": True,
        "メッセージ": (
            f"1レース上限{pct:.1f}%なら、破産確率を{req.max_ruin_probability_pct}%以下に抑えられます。"
            + ("(実績データを使ったブートストラップ方式)" if use_bootstrap else "(実績データ不足のため入力値をそのまま使う方式)")
        ),
        "推奨_1レース上限%": round(pct, 1),
        "その上限での破産確率%": sim["ruin_probability_pct"],
        "黒字化率%": sim["profit_probability_pct"],
        "平均最終資金": sim["average_final_bankroll"],
        "方式": "bootstrap" if use_bootstrap else "average",
    }
