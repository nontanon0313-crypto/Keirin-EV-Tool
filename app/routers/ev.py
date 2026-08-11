import itertools
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from .. import ev_calculator as calc
from . import bankroll as bankroll_router
from . import purchases as purchases_router

router = APIRouter(prefix="/ev", tags=["ev"])


def _build_win_probs(entries: List[models.Entry]) -> dict:
    probs = {}
    for e in entries:
        if e.blended_win_prob is not None:
            probs[e.car_number] = e.blended_win_prob
    # 念のため合計1に正規化
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs


def _estimate_prob(win_probs: dict, bet_type: str, cars: tuple, line_map: dict = None, line_boost: float = 1.0) -> float:
    """券種ごとに正しい的中確率の計算方法を呼び分ける。"""
    if bet_type == "複勝":
        return calc.fukusho_prob(win_probs, cars[0], line_map, line_boost)
    if bet_type == "ワイド":
        return calc.wide_prob(win_probs, cars[0], cars[1], line_map, line_boost)
    ordered = bet_type in calc.ORDERED_BET_TYPES
    return calc.combination_prob(win_probs, cars, ordered, line_map, line_boost)


def _apply_calibration(est_prob: float, calibration_factors: dict) -> float:
    """
    購入実績に基づく自動補正係数を適用する。
    該当する勝率帯の試行数が十分(200÷帯の代表確率)でなければ、補正せずそのまま返す。
    """
    bucket_name, _ = calc.get_prob_bucket(est_prob)
    info = calibration_factors.get(bucket_name)
    if not info or not info["is_reliable"]:
        return est_prob
    calibrated = est_prob * info["calibration_factor"]
    return max(0.0, min(1.0, calibrated))


@router.post("/calculate/{race_id}")
def calculate_ev(race_id: int, req: schemas.EvCalcRequest, db: Session = Depends(get_db)):
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    if not entries:
        raise HTTPException(400, "選手データがありません。出走表のスクショを読み込ませてください")

    win_probs = _build_win_probs(entries)
    if not win_probs:
        raise HTTPException(
            400,
            "選手の勝率データが1件も揃っていません。「勝率」画面(tipstar等)のスクショを追加で読み込ませるか、"
            "解析処理でAI推定に失敗している可能性があります"
        )
    missing_count = len(entries) - len(win_probs)
    if missing_count > 0:
        raise HTTPException(
            400,
            f"{len(entries)}名中{missing_count}名の勝率データが未取得です。全員分揃うまでレース詳細で確認してください"
        )

    bankroll = req.bankroll if req.bankroll is not None else bankroll_router.get_current_balance(db)
    odds_rows = db.query(models.Odds).filter(models.Odds.race_id == race_id).all()
    if not odds_rows:
        raise HTTPException(400, "オッズデータがありません。オッズ画面(全券種)のスクショを読み込ませてください")

    # 券種ごとにオッズをまとめ、正規化した市場確率を使えるようにする
    by_bet_type = {}
    for o in odds_rows:
        by_bet_type.setdefault(o.bet_type, {})[o.combination] = o.odds_value

    normalized_market = {}
    for bet_type, odds_map in by_bet_type.items():
        normalized_market[bet_type] = calc.normalize_market_probs(odds_map)

    # 既存の未反映ev_resultsは作り直す
    db.query(models.EvResult).filter(models.EvResult.race_id == race_id).delete()

    calibration_factors = purchases_router.get_calibration_factors(db)

    created = []
    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob = _estimate_prob(win_probs, o.bet_type, cars)
        est_prob = _apply_calibration(est_prob, calibration_factors)

        market_prob = normalized_market.get(o.bet_type, {}).get(
            o.combination, calc.market_prob_from_odds(o.odds_value, o.bet_type)
        )

        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value)
        is_skip, skip_reason = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)
        # 100円ベット換算で、安全マージン(オッズ変動対策)を考慮した閾値以上を「買い示唆」とする
        is_recommended = (not is_skip) and (ev_pct >= req.min_ev_pct)

        stake_info = calc.recommend_stake(
            bankroll, est_prob, o.odds_value, req.fractional_coefficient, req.max_bet_pct_per_bet
        )

        ev_result = models.EvResult(
            race_id=race_id,
            bet_type=o.bet_type,
            combination=o.combination,
            estimated_win_prob=est_prob,
            market_prob=market_prob,
            odds_value=o.odds_value,
            ev_pct=ev_pct,
            kelly_fraction=stake_info["kelly_fraction_capped"],
            recommended_stake=0 if is_skip else stake_info["recommended_stake"],
            is_skip=is_skip,
            skip_reason=skip_reason,
            is_recommended=is_recommended,
        )
        db.add(ev_result)
        created.append(ev_result)

    db.commit()

    odds_lookup = {(o.bet_type, o.combination): o.total_vote_amount for o in odds_rows}

    # 買い示唆を最優先、その中で期待値が高い順に並べる。次に見送りを末尾に回す。
    results = sorted(
        created,
        key=lambda r: (not r.is_recommended, r.is_skip, -r.ev_pct),
    )

    output_results = []
    for r in results:
        total_vote = odds_lookup.get((r.bet_type, r.combination))
        stake = r.recommended_stake or 0
        if total_vote is not None and total_vote > 0:
            self_impact_pct = round(stake / (total_vote + stake) * 100, 2)
        else:
            self_impact_pct = None
        output_results.append({
            "bet_type": r.bet_type,
            "combination": r.combination,
            "estimated_win_prob_pct": round(r.estimated_win_prob * 100, 2),
            "market_prob_pct": round(r.market_prob * 100, 2),
            "odds_value": r.odds_value,
            "ev_pct": round(r.ev_pct, 2),
            "recommended_stake": r.recommended_stake,
            "is_skip": r.is_skip,
            "skip_reason": r.skip_reason,
            "is_recommended": r.is_recommended,
            "self_impact_pct": self_impact_pct,
            "self_impact_warning": self_impact_pct is not None and self_impact_pct >= 2.0,
        })

    return {
        "race_id": race_id,
        "bankroll": bankroll,
        "total_combinations": len(results),
        "results": output_results,
    }


@router.post("/threshold-table/{race_id}")
def threshold_table(
    race_id: int,
    min_ev_pct: float = 5.0,
    min_win_prob: float = 0.05,
    limit: int = 15,
    db: Session = Depends(get_db),
):
    """
    オッズが無くても、選手データ(勝率推定)だけから「このオッズ以上なら買い」という
    閾値オッズ表を作成する。出走表は締切まで変わらないため、これを先に作っておけば、
    投票直前はオッズ画面と見比べるだけで判断できる(オッズのスクショ解析が不要になる)。

    閾値オッズが低い(=少しのオッズでも買い成立する)順に並べ、上位limit件に絞る。
    全組み合わせは膨大なため、実際に投票直前のオッズ画面(人気順表示)でチェックすべき
    「注目リスト」として使うことを想定している。
    閾値オッズ = (1 + 安全マージン%/100) ÷ 推定的中確率
    """
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    win_probs = _build_win_probs(entries)
    if not win_probs:
        raise HTTPException(400, "選手の勝率データが揃っていません")

    car_numbers = list(win_probs.keys())
    calibration_factors = purchases_router.get_calibration_factors(db)

    results = []
    for bet_type, arity in calc.BET_TYPE_ARITY.items():
        if len(car_numbers) < arity:
            continue
        ordered = bet_type in calc.ORDERED_BET_TYPES
        combos = (
            itertools.permutations(car_numbers, arity)
            if ordered
            else itertools.combinations(car_numbers, arity)
        )
        for combo in combos:
            est_prob = _estimate_prob(win_probs, bet_type, combo)
            est_prob = _apply_calibration(est_prob, calibration_factors)
            is_skip, _ = calc.apply_min_prob_filter(est_prob, 100, min_win_prob)  # 勝率フィルターのみ判定
            if is_skip or est_prob <= 0:
                continue
            threshold_odds = (1 + min_ev_pct / 100) / est_prob
            results.append({
                "bet_type": bet_type,
                "combination": "-".join(str(c) for c in combo),
                "estimated_win_prob_pct": round(est_prob * 100, 2),
                "threshold_odds": round(threshold_odds, 2),
            })

    results.sort(key=lambda r: r["threshold_odds"])
    total_combinations = len(results)
    results = results[:limit]

    return {
        "race_id": race_id,
        "min_ev_pct": min_ev_pct,
        "min_win_prob_pct": min_win_prob * 100,
        "total_combinations": total_combinations,
        "shown_count": len(results),
        "message": (
            f"全{total_combinations}通り中、閾値オッズが低い(=買いが成立しやすい)上位{len(results)}点を表示。"
            "表示されているオッズが「閾値オッズ」以上なら買い示唆です。"
        ),
        "results": results,
    }


@router.post("/race-plan/{race_id}")
def race_plan(race_id: int, req: schemas.RacePlanRequest, db: Session = Depends(get_db)):
    """
    1レース全体で、期待値プラス(安全マージン込み)の買い目をまとめて拾い、
    証拠金・1レース上限比率の範囲内に収まるよう自動で配分する「投票プラン」を返す。
    複数の買い目は互いに同時的中しない(排反)とみなして単純合算する簡易モデル。
    """
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    win_probs = _build_win_probs(entries)
    if not win_probs:
        raise HTTPException(400, "選手の勝率データが揃っていません")

    bankroll = req.bankroll if req.bankroll is not None else bankroll_router.get_current_balance(db)
    odds_rows = db.query(models.Odds).filter(models.Odds.race_id == race_id).all()
    if not odds_rows:
        raise HTTPException(400, "オッズデータがありません")

    candidates = []
    calibration_factors = purchases_router.get_calibration_factors(db)
    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob = _estimate_prob(win_probs, o.bet_type, cars)
        est_prob = _apply_calibration(est_prob, calibration_factors)
        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value)
        is_skip, _ = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)
        is_recommended = (not is_skip) and (ev_pct >= req.min_ev_pct)
        if not is_recommended:
            continue

        f = calc.kelly_fraction(est_prob, o.odds_value, req.fractional_coefficient)
        f_capped = min(f, req.max_bet_pct_per_bet)
        raw_stake = bankroll * f_capped
        candidates.append({
            "bet_type": o.bet_type,
            "combination": o.combination,
            "estimated_win_prob_pct": round(est_prob * 100, 2),
            "odds_value": o.odds_value,
            "ev_pct": round(ev_pct, 2),
            "raw_stake": raw_stake,
            "win_prob": est_prob,
            "total_vote_amount": o.total_vote_amount,
        })

    if not candidates:
        return {
            "race_id": race_id,
            "message": "安全マージンを満たす買い示唆がありませんでした(見送り推奨)",
            "items": [],
            "total_stake": 0,
        }

    total_raw_stake = sum(c["raw_stake"] for c in candidates)
    race_cap = bankroll * req.max_race_pct
    scale = min(1.0, race_cap / total_raw_stake) if total_raw_stake > 0 else 1.0

    items = []
    total_stake = 0.0
    total_expected_profit = 0.0
    for c in sorted(candidates, key=lambda x: -x["ev_pct"]):
        stake = round(c["raw_stake"] * scale, 0)
        expected_profit = stake * (c["win_prob"] * c["odds_value"] - 1.0)
        total_stake += stake
        total_expected_profit += expected_profit
        total_vote = c["total_vote_amount"]
        if total_vote is not None and total_vote > 0:
            self_impact_pct = round(stake / (total_vote + stake) * 100, 2)
        else:
            self_impact_pct = None
        items.append({
            "bet_type": c["bet_type"],
            "combination": c["combination"],
            "estimated_win_prob_pct": c["estimated_win_prob_pct"],
            "odds_value": c["odds_value"],
            "ev_pct": c["ev_pct"],
            "stake": stake,
            "self_impact_pct": self_impact_pct,
            "self_impact_warning": self_impact_pct is not None and self_impact_pct >= 2.0,
        })

    return {
        "race_id": race_id,
        "num_bets": len(items),
        "total_stake": round(total_stake, 0),
        "race_budget_cap": round(race_cap, 0),
        "was_scaled_down": scale < 1.0,
        "total_expected_profit": round(total_expected_profit, 0),
        "race_ev_pct": round((total_expected_profit / total_stake * 100), 2) if total_stake > 0 else 0,
        "items": items,
    }


@router.post("/box-suggestion/{race_id}")
def box_suggestion(
    race_id: int,
    bet_type: str,
    car_numbers: List[int],
    bankroll: float,
    fractional_coefficient: float = 0.25,
    max_total_pct: float = 0.10,
    db: Session = Depends(get_db),
):
    """
    指定した車番の組み合わせをBOX(全通り)で購入した場合の、合計期待値と推奨配分を計算する。
    max_total_pct: BOX全体で使う金額の資金に対する上限比率
    """
    race = db.query(models.Race).get(race_id)
    if not race:
        raise HTTPException(404, "レースが見つかりません")

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    win_probs = _build_win_probs(entries)

    arity = calc.BET_TYPE_ARITY.get(bet_type)
    if not arity:
        raise HTTPException(400, "不明な券種です")
    if len(car_numbers) < arity:
        raise HTTPException(400, f"{bet_type}には最低{arity}車必要です")

    ordered = bet_type in calc.ORDERED_BET_TYPES
    combos = (
        list(itertools.permutations(car_numbers, arity))
        if ordered
        else list(itertools.combinations(car_numbers, arity))
    )

    odds_rows = {
        o.combination: o.odds_value
        for o in db.query(models.Odds).filter(
            models.Odds.race_id == race_id, models.Odds.bet_type == bet_type
        )
    }

    box_items = []
    total_ev_weighted = 0.0
    total_kelly = 0.0
    for combo in combos:
        combo_str = "-".join(str(c) for c in combo)
        odds_value = odds_rows.get(combo_str)
        if odds_value is None:
            continue
        est_prob = _estimate_prob(win_probs, bet_type, combo)
        ev_pct = calc.calc_ev_pct(est_prob, odds_value)
        f = calc.kelly_fraction(est_prob, odds_value, fractional_coefficient)
        box_items.append({
            "combination": combo_str,
            "estimated_win_prob_pct": round(est_prob * 100, 2),
            "odds_value": odds_value,
            "ev_pct": round(ev_pct, 2),
            "kelly_fraction": f,
        })
        total_ev_weighted += est_prob * odds_value
        total_kelly += f

    if not box_items:
        raise HTTPException(400, "指定した組み合わせに対応するオッズが見つかりません")

    # BOX全体の合成期待値(等額購入した場合の資金1単位あたりの期待払戻)
    n = len(box_items)
    box_ev_pct = (total_ev_weighted / n - 1.0) * 100.0

    # 資金配分: 各点のケリー比率に応じて按分し、BOX全体でmax_total_pctを超えないようキャップ
    capped_total_pct = min(total_kelly, max_total_pct)
    for item in box_items:
        share = (item["kelly_fraction"] / total_kelly) if total_kelly > 0 else (1 / n)
        item["recommended_stake"] = round(bankroll * capped_total_pct * share, 0)

    return {
        "race_id": race_id,
        "bet_type": bet_type,
        "box_cars": car_numbers,
        "num_combinations": n,
        "box_ev_pct": round(box_ev_pct, 2),
        "total_recommended_stake": round(bankroll * capped_total_pct, 0),
        "items": sorted(box_items, key=lambda x: -x["ev_pct"]),
    }
