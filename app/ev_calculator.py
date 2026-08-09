"""
期待値計算のコアロジック

用語:
- win_prob: 各選手が「1着になる」確率 (0-1)
- 組み合わせ確率(例: 3連単 1-2-3): Harville式で1着×2着×3着の同時確率を推定
- market_prob: オッズから逆算した「市場が織り込んでいる確率」
- ev_pct: 期待値(%) = (組み合わせ確率 × オッズ - 1) × 100
- kelly_fraction: フラクショナルケリーによる推奨賭け比率
"""

import itertools
import random
from typing import List, Dict, Tuple

# 競輪の券種別 控除率(dev注: 実際の値は年度・券種で変わるため、確定情報がない場合の暫定値。
# オッズが全買い目分揃っていれば正規化で代替できるためこの値はあくまでフォールバック)
DEFAULT_TAKEOUT_RATE = {
    "単勝": 0.20,
    "複勝": 0.20,
    "2車単": 0.25,
    "2車複": 0.25,
    "2枠単": 0.25,
    "2枠複": 0.25,
    "ワイド": 0.25,
    "3連単": 0.275,
    "3連複": 0.275,
}

# 券種ごとに何着分の車番が必要か
BET_TYPE_ARITY = {
    "単勝": 1,
    "複勝": 1,
    "2車単": 2,
    "2車複": 2,
    "2枠単": 2,
    "2枠複": 2,
    "ワイド": 2,
    "3連単": 3,
    "3連複": 3,
}

# 着順を区別するか(単/複で結果が変わるか)
ORDERED_BET_TYPES = {"単勝", "2車単", "2枠単", "3連単"}


def harville_prob(win_probs: Dict[int, float], order: Tuple[int, ...]) -> float:
    """
    Harville式で「orderで指定した順に着順が決まる確率」を計算する。
    win_probs: {車番: 1着になる確率}
    order: 例 (1,2,3) -> 車番1が1着、2が2着、3が3着になる確率

    P(1着=a) = p_a
    P(2着=b | 1着=a) = p_b / (1 - p_a)
    P(3着=c | 1着=a,2着=b) = p_c / (1 - p_a - p_b)
    """
    remaining = 1.0
    prob = 1.0
    used = []
    for car in order:
        p = win_probs.get(car, 0.0)
        denom = remaining
        if denom <= 1e-9:
            return 0.0
        prob *= p / denom
        remaining -= p
        used.append(car)
    return max(prob, 0.0)


def combination_prob(win_probs: Dict[int, float], cars: Tuple[int, ...], ordered: bool) -> float:
    """
    指定した車番の組み合わせが「その通りに」または「着順不問(複)で」決まる確率。
    ordered=True: 3連単/2車単等、着順通りの確率のみ
    ordered=False: 3連複/2車複/ワイド等、順不同で合算
    """
    if ordered:
        return harville_prob(win_probs, cars)
    else:
        total = 0.0
        for perm in itertools.permutations(cars):
            total += harville_prob(win_probs, perm)
        return total


def market_prob_from_odds(odds_value: float, bet_type: str, use_takeout_fallback: bool = True) -> float:
    """
    単一のオッズ値から市場確率を概算する(簡易版)。
    本来は同一レース・同一券種の全オッズを集めて正規化するのが望ましい。
    正規化データが無い場合のフォールバックとして、控除率を使った近似を返す。
    """
    if odds_value <= 0:
        return 0.0
    takeout = DEFAULT_TAKEOUT_RATE.get(bet_type, 0.25)
    raw_prob = 1.0 / odds_value
    if use_takeout_fallback:
        return raw_prob * (1 - takeout)
    return raw_prob


def normalize_market_probs(odds_map: Dict[str, float]) -> Dict[str, float]:
    """
    同一券種の全買い目のオッズが揃っている場合、1/oddsの合計で正規化して
    より正確な市場確率を算出する。
    odds_map: {combination: odds_value}
    """
    raw = {k: (1.0 / v if v > 0 else 0.0) for k, v in odds_map.items()}
    total = sum(raw.values())
    if total <= 0:
        return {k: 0.0 for k in odds_map}
    return {k: v / total for k, v in raw.items()}


def calc_ev_pct(estimated_prob: float, odds_value: float) -> float:
    """期待値%。 (推定確率 × オッズ - 1) × 100"""
    return (estimated_prob * odds_value - 1.0) * 100.0


def kelly_fraction(win_prob: float, odds_value: float, fractional_coefficient: float = 0.25) -> float:
    """
    フラクショナルケリー基準による推奨賭け比率(資金に対する割合)を返す。
    b = odds_value - 1 (純利益倍率)
    f* = (b*p - q) / b   (q = 1-p)
    マイナスになる場合(期待値マイナス)は0を返す。
    """
    b = odds_value - 1.0
    if b <= 0:
        return 0.0
    p = win_prob
    q = 1.0 - p
    f_full = (b * p - q) / b
    if f_full <= 0:
        return 0.0
    return f_full * fractional_coefficient


def recommend_stake(
    bankroll: float,
    win_prob: float,
    odds_value: float,
    fractional_coefficient: float = 0.25,
    max_bet_pct_per_bet: float = 0.05,
) -> Dict[str, float]:
    """
    推奨購入金額を計算する。
    max_bet_pct_per_bet: 1点あたりの上限比率(資金に対する割合)。ケリー計算値がこれを超えたらキャップする。
    """
    f = kelly_fraction(win_prob, odds_value, fractional_coefficient)
    capped_f = min(f, max_bet_pct_per_bet)
    stake = bankroll * capped_f
    return {
        "kelly_fraction_raw": f,
        "kelly_fraction_capped": capped_f,
        "recommended_stake": round(stake, 0),
    }


def apply_min_prob_filter(win_prob: float, ev_pct: float, min_win_prob: float = 0.05) -> Tuple[bool, str]:
    """
    最低勝率フィルター。期待値がプラスでも、勝率が低すぎる(大穴頼み)場合は見送り推奨とする。
    戻り値: (見送りにすべきか, 理由)
    """
    if ev_pct <= 0:
        return True, "期待値マイナス"
    if win_prob < min_win_prob:
        return True, f"勝率が最低ライン({min_win_prob*100:.1f}%)未満(大穴頼み)"
    return False, ""


def monte_carlo_bankruptcy(
    initial_bankroll: float,
    win_prob: float,
    odds_value: float,
    stake_fraction: float,
    num_bets_per_trial: int = 100,
    num_trials: int = 5000,
    ruin_threshold_pct: float = 0.5,
    seed: int = 42,
) -> Dict[str, float]:
    """
    同一の勝率・オッズ・賭け比率で繰り返し賭け続けた場合の資金推移をシミュレーションし、
    「初期資金のruin_threshold_pct(例:50%)以下まで減る確率」を推定する。

    単純化のため、1試行=同一の win_prob/odds/stake_fraction を num_bets_per_trial 回繰り返す
    モデル(複数の異なる買い目を混在させる場合は呼び出し側で加重平均値を渡すか、
    複数パターンをまとめてシミュレーションする拡張が必要)。
    """
    rng = random.Random(seed)
    ruin_count = 0
    final_bankrolls = []

    for _ in range(num_trials):
        bankroll = initial_bankroll
        ruined = False
        for _ in range(num_bets_per_trial):
            if bankroll <= 0:
                ruined = True
                break
            stake = bankroll * stake_fraction
            if rng.random() < win_prob:
                bankroll += stake * (odds_value - 1.0)
            else:
                bankroll -= stake
            if bankroll <= initial_bankroll * ruin_threshold_pct:
                ruined = True
                # 破産扱いにするが、シミュレーションは最後まで続ける(以後もそのまま推移させる)
        if ruined:
            ruin_count += 1
        final_bankrolls.append(bankroll)

    avg_final = sum(final_bankrolls) / len(final_bankrolls)
    return {
        "ruin_probability_pct": round(ruin_count / num_trials * 100, 2),
        "average_final_bankroll": round(avg_final, 0),
        "num_trials": num_trials,
        "num_bets_per_trial": num_bets_per_trial,
        "ruin_threshold_pct": ruin_threshold_pct,
    }
