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
    if bet_type == "ワイド":
        return calc.wide_prob(win_probs, cars[0], cars[1], line_map, line_boost)
    ordered = bet_type in calc.ORDERED_BET_TYPES
    return calc.combination_prob(win_probs, cars, ordered, line_map, line_boost)


def _apply_calibration(est_prob: float, calibration_factors: dict) -> tuple:
    """
    購入実績に基づく自動補正係数を適用する。
    該当する勝率帯の試行数が十分(200÷帯の代表確率)でなければ、補正せずそのまま返す。
    戻り値: (補正後確率, その勝率帯の低確率帯かつ未補正=推定誤差に注意すべきか)
    """
    bucket_name, _ = calc.get_prob_bucket(est_prob)
    info = calibration_factors.get(bucket_name)
    is_reliable = bool(info and info["is_reliable"])
    # オッズが跳ねる低確率帯(0-5%=大穴)は、確率推定のわずかな誤差がEVの計算上
    # 大きく増幅されるため、実績による補正が効いていない間は特に注意が必要(のんとの
    # 相談により追加)。
    low_prob_warning = (bucket_name == "0-5%(大穴)") and not is_reliable
    if not info or not is_reliable:
        return est_prob, low_prob_warning
    calibrated = est_prob * info["calibration_factor"]
    return max(0.0, min(1.0, calibrated)), low_prob_warning


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
    low_prob_warnings = {}
    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob = _estimate_prob(win_probs, o.bet_type, cars)
        est_prob, low_prob_warning = _apply_calibration(est_prob, calibration_factors)
        low_prob_warnings[(o.bet_type, o.combination)] = low_prob_warning

        market_prob = normalized_market.get(o.bet_type, {}).get(
            o.combination, calc.market_prob_from_odds(o.odds_value, o.bet_type)
        )

        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value, req.rebate_pct)
        is_skip, skip_reason = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)
        # 100円ベット換算で、安全マージン(オッズ変動対策)を考慮した閾値以上を「買い示唆」とする
        is_recommended = (not is_skip) and (ev_pct >= req.min_ev_pct)

        stake_info = calc.recommend_stake(
            bankroll, est_prob, o.odds_value, req.fractional_coefficient, req.max_bet_pct_per_bet, req.rebate_pct
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
    hidden_count = 0
    for r in results:
        # 期待値マイナス・見送り対象は一覧に出さない(ノイズになるため)
        if r.is_skip or r.ev_pct < 0:
            hidden_count += 1
            continue
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
            "roi_pct": round(r.ev_pct + 100, 2),
            "recommended_stake": r.recommended_stake,
            "is_skip": r.is_skip,
            "skip_reason": r.skip_reason,
            "is_recommended": r.is_recommended,
            "self_impact_pct": self_impact_pct,
            "self_impact_warning": self_impact_pct is not None and self_impact_pct >= 2.0,
            "low_prob_warning": low_prob_warnings.get((r.bet_type, r.combination), False),
        })

    return {
        "race_id": race_id,
        "bankroll": bankroll,
        "hidden_negative_count": hidden_count,
        "total_combinations": len(results),
        "results": output_results,
    }


@router.post("/threshold-table/{race_id}")
def threshold_table(
    race_id: int,
    min_ev_pct: float = 5.0,
    min_win_prob: float = 0.05,
    limit: int = 15,
    rebate_pct: float = 0.0,
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
            est_prob, low_prob_warning = _apply_calibration(est_prob, calibration_factors)
            is_skip, _ = calc.apply_min_prob_filter(est_prob, 100, min_win_prob)  # 勝率フィルターのみ判定
            if is_skip or est_prob <= 0:
                continue
            threshold_odds = (1 + min_ev_pct / 100 - rebate_pct) / est_prob
            results.append({
                "bet_type": bet_type,
                "combination": "-".join(str(c) for c in combo),
                "estimated_win_prob_pct": round(est_prob * 100, 2),
                "threshold_odds": round(threshold_odds, 2),
                "low_prob_warning": low_prob_warning,
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
    avoid_garami=True(既定)の場合、券種をまたいで買い目を選ぶことで発生しうる
    「的中したのに合計投票額を下回る(ガミる)」結果が起きないことを保証しながら選定する。
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
        est_prob, low_prob_warning = _apply_calibration(est_prob, calibration_factors)
        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value, req.rebate_pct)
        is_skip, _ = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)
        is_recommended = (not is_skip) and (ev_pct >= req.min_ev_pct)
        if not is_recommended:
            continue

        f = calc.kelly_fraction(est_prob, o.odds_value, req.fractional_coefficient, req.rebate_pct)
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
            "low_prob_warning": low_prob_warning,
        })

    excluded_low_prob_count = 0
    if req.exclude_low_prob_warning:
        before_count = len(candidates)
        candidates = [c for c in candidates if not c["low_prob_warning"]]
        excluded_low_prob_count = before_count - len(candidates)

    if not candidates:
        return {
            "race_id": race_id,
            "message": "安全マージンを満たす買い示唆がありませんでした(見送り推奨)",
            "items": [],
            "total_stake": 0,
        }

    race_cap = bankroll * req.max_race_pct

    # --- ガミり(的中はしたのに合計投票額を下回る)を避けるための「起こりうる結果」の洗い出し ---
    # 券種をまたいで買い目を選ぶと、同じ結果で複数券が同時的中したり、逆に1つしか
    # 的中しなかったりする。3連単の着順(上位3着の並び)がどの券種の的中・不的中も
    # 一意に決めるため、これを「起こりうる結果」の全体集合として使う。
    car_numbers = sorted({e.car_number for e in entries})
    outcomes = list(itertools.permutations(car_numbers, 3)) if len(car_numbers) >= 3 else []
    # 各結果(起こりうる着順)の確率。3連単の的中確率と全く同じ計算(Harville式)。
    outcome_probs = {o: _estimate_prob(win_probs, "3連単", o) for o in outcomes} if outcomes else {}

    # 投票時オッズは締切までにズレる(券種によってズレ幅が大きく異なり、実測でワイドは
    # 3連単の4倍近くズレることが分かっている)。ガミり判定はこのズレを見込んで、
    # 実際のオッズより保守的な値で計算し、多少ズレても崩れない安全マージンを持たせる。
    odds_safety_margins = purchases_router.get_odds_safety_margins(db)

    def _winning_outcomes(bet_type: str, cars: tuple) -> list:
        combination_str = "-".join(str(c) for c in cars)
        return [o for o in outcomes if calc.judge_purchase_result(bet_type, combination_str, list(o))]

    # 期待値が高い順に、ガミりが起きない範囲で・100円単位で予算内に収まる分だけ採用する。
    items = []
    total_stake = 0.0
    total_expected_profit = 0.0
    remaining_budget = race_cap
    payout_by_outcome = {o: 0.0 for o in outcomes}
    excluded_by_garami_count = 0
    excluded_by_budget_count = 0

    for c in sorted(candidates, key=lambda x: -x["ev_pct"]):
        if req.max_items and len(items) >= req.max_items:
            break
        stake = calc.round_to_bet_unit(c["raw_stake"])
        if stake > remaining_budget:
            if remaining_budget < 100:
                break  # もう1点(最低100円)も買う余地が無いので、以降は全て予算オーバー
            stake = calc.round_to_bet_unit(remaining_budget)
            if stake <= 0 or stake > remaining_budget:
                excluded_by_budget_count += 1
                continue

        cars = tuple(int(x) for x in c["combination"].split("-"))
        winning = _winning_outcomes(c["bet_type"], cars) if req.avoid_garami else []

        if req.avoid_garami:
            if not winning:
                # 起こりうる結果と1つも一致しない(データ不整合)場合は安全側でスキップ
                excluded_by_garami_count += 1
                continue
            new_total_stake = total_stake + stake
            margin_pct = odds_safety_margins.get(c["bet_type"], purchases_router.DEFAULT_ODDS_SAFETY_MARGIN_PCT)
            safety_odds = c["odds_value"] * (1 - margin_pct / 100)
            payout_gain = stake * safety_odds
            ok = True
            # このベットが的中する結果それぞれで、payoutが新しい合計投票額を下回らないか確認
            for o in winning:
                if payout_by_outcome[o] + payout_gain < new_total_stake:
                    ok = False
                    break
            # 既存の的中結果(このベットでは的中しないもの)も、合計投票額が増える分だけ
            # 再チェックが必要
            if ok:
                for o, payout in payout_by_outcome.items():
                    if payout > 0 and o not in winning and payout < new_total_stake:
                        ok = False
                        break
            if not ok:
                excluded_by_garami_count += 1
                continue
            for o in winning:
                payout_by_outcome[o] += payout_gain

        expected_profit = stake * (c["win_prob"] * c["odds_value"] - 1.0)
        total_stake += stake
        total_expected_profit += expected_profit
        remaining_budget -= stake
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
            "roi_pct": round(c["ev_pct"] + 100, 2),
            "stake": stake,
            "self_impact_pct": self_impact_pct,
            "self_impact_warning": self_impact_pct is not None and self_impact_pct >= 2.0,
            "low_prob_warning": c["low_prob_warning"],
        })

    race_ev_pct = round((total_expected_profit / total_stake * 100), 2) if total_stake > 0 else 0

    # 「レース全体の的中率」= 採用した買い目のうち、どれか1つでも的中する結果の確率の合計。
    # avoid_garami有効時は、的中する結果=必ず黒字になる結果でもある(ガミりを起こさないよう
    # 選んでいるため)。以前は各買い目の勝率を単純合算していたが、券種をまたいで同じ結果が
    # 重複カウントされるケースがあり不正確だった(のんの指摘により修正)。
    if outcomes:
        race_hit_prob_pct = round(
            sum(p for o, p in outcome_probs.items() if payout_by_outcome.get(o, 0.0) > 0) * 100, 2
        )
    else:
        race_hit_prob_pct = 0

    return {
        "race_id": race_id,
        "num_bets": len(items),
        "total_stake": round(total_stake, 0),
        "race_budget_cap": round(race_cap, 0),
        "excluded_by_budget_count": excluded_by_budget_count,
        "excluded_by_garami_count": excluded_by_garami_count,
        "excluded_low_prob_count": excluded_low_prob_count,
        "garami_free": req.avoid_garami,
        "odds_safety_margins_used_pct": odds_safety_margins if req.avoid_garami else {},
        "total_expected_profit": round(total_expected_profit, 0),
        "race_ev_pct": race_ev_pct,
        "race_roi_pct": round(race_ev_pct + 100, 2),
        "race_hit_prob_pct": race_hit_prob_pct,
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
