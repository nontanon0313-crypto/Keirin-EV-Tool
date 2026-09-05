import itertools
import time as _time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from .. import ev_calculator as calc
from . import purchases as purchases_router
from . import bankroll as bankroll_router

router = APIRouter(prefix="/ev", tags=["ev"])


def _build_win_probs(entries: List[models.Entry]) -> dict:
    """blended → ai → tipstar の順で勝率を拾う（replay/再プラン用）。"""
    probs = {}
    for e in entries:
        v = e.blended_win_prob
        if v is None:
            v = getattr(e, "ai_win_prob", None)
        if v is None:
            v = getattr(e, "tipstar_win_prob", None)
        if v is not None:
            try:
                probs[e.car_number] = float(v)
            except (TypeError, ValueError):
                pass
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs



def _line_map_from_race(race) -> tuple:
    """race.lines_data から line_map と line_boost を返す。"""
    line_map = None
    line_boost = 1.2
    if race and race.lines_data:
        line_map = {}
        for idx, line in enumerate(race.lines_data):
            for car in line:
                try:
                    line_map[int(car)] = idx
                except (TypeError, ValueError):
                    pass
        if not line_map:
            line_map = None
    return line_map, line_boost

def _estimate_prob(win_probs: dict, bet_type: str, cars: tuple, line_map: dict = None, line_boost: float = 1.0) -> float:
    """券種ごとに正しい的中確率の計算方法を呼び分ける。"""
    if bet_type == "ワイド":
        return calc.wide_prob(win_probs, cars[0], cars[1], line_map, line_boost)
    ordered = bet_type in calc.ORDERED_BET_TYPES
    return calc.combination_prob(win_probs, cars, ordered, line_map, line_boost)



def _apply_calibration(est_prob: float, calibration_factors: dict, bet_type: str = None):
    """
    勝率帯・券種交差の補正係数を掛けた確率を返す。
    戻り値: (補正後確率, low_prob_warning, data_sufficiency_pct, prediction_accuracy_pct)
    """
    bucket_name, _ = calc.get_prob_bucket(est_prob)
    info = (calibration_factors or {}).get(bucket_name)
    overall = (calibration_factors or {}).get("overall")

    MIN_CROSS_SAMPLE = 30
    cross_map = (calibration_factors or {}).get("by_bet_type_bucket") or {}
    cross_info = (cross_map.get(bet_type) or {}).get(bucket_name) if bet_type else None
    cross_used = False

    factor = 1.0
    data_sufficiency_pct = 0.0
    accuracy_pct = None

    if cross_info and cross_info.get("sample_count", 0) >= MIN_CROSS_SAMPLE and cross_info.get("calibration_factor") is not None:
        factor = float(cross_info["calibration_factor"])
        cross_used = True
        req = max(int(cross_info.get("required_sample_count") or 1), 1)
        data_sufficiency_pct = min(100.0, 100.0 * float(cross_info.get("sample_count") or 0) / req)
        accuracy_pct = cross_info.get("prediction_accuracy_pct")
    elif info and info.get("sample_count", 0) >= 80 and info.get("calibration_factor") is not None:
        factor = float(info["calibration_factor"])
        req = max(int(info.get("required_sample_count") or 1), 1)
        data_sufficiency_pct = min(100.0, 100.0 * float(info.get("sample_count") or 0) / req)
        accuracy_pct = info.get("prediction_accuracy_pct")
    elif overall and overall.get("calibration_factor") is not None:
        factor = float(overall["calibration_factor"])
        req = max(int(overall.get("required_sample_count") or 1), 1)
        data_sufficiency_pct = min(100.0, 100.0 * float(overall.get("sample_count") or 0) / req)
        accuracy_pct = overall.get("prediction_accuracy_pct")

    if not cross_used:
        by_bt = (calibration_factors or {}).get("by_bet_type") or {}
        if bet_type and bet_type in by_bt and overall and overall.get("calibration_factor"):
            bt_f = float(by_bt[bet_type]["calibration_factor"])
            ov_f = float(overall["calibration_factor"])
            if ov_f > 1e-9:
                residual = bt_f / ov_f
                residual = 1.0 + 0.5 * (residual - 1.0)
                factor *= residual

    # 下限0.25は帯校正用。購入集合の追加係数は別関数で掛ける
    factor = max(0.25, min(2.0, factor))
    calibrated = est_prob if abs(factor - 1.0) < 1e-9 else max(0.0, min(1.0, est_prob * factor))
    low_prob_warning = calibrated < 0.05
    return calibrated, low_prob_warning, round(data_sufficiency_pct, 1), accuracy_pct


def _odds_band_key(odds: float) -> str:
    """実績ゲート・購入集合残差と揃えた細分化オッズ帯。"""
    if odds is None or odds <= 0:
        return "不明"
    if odds < 5:
        return "1-5倍"
    if odds < 10:
        return "5-10倍"
    if odds < 30:
        return "10-30倍"
    if odds < 100:
        return "30-100倍"
    if odds < 300:
        return "100-300倍"
    if odds < 1000:
        return "300-1000倍"
    if odds < 3000:
        return "1000-3000倍"
    return "3000倍以上"


def _apply_purchase_set_factor(est_prob: float, odds_value: float, bet_type: str, purchase_factors: dict) -> float:
    """
    実購入集合で観測された「予測p vs 実績的中」から求めた追加係数。

    優先（具体 → 粗い）:
      券種×オッズ帯 → オッズ帯 → 券種 → 全体
    さらに券種係数がある場合は **上限として券種係数を掛ける（cap）**。
    - 2車単のように券種全体が悪いのに帯だけ甘い、を防ぐ
    - 3連単のように券種が妥当なのに overall の min で潰す、も防ぐ
    """
    if not purchase_factors or est_prob is None or est_prob <= 0:
        return est_prob
    band = _odds_band_key(float(odds_value) if odds_value else 0)

    factor = None
    cross = (purchase_factors.get("by_bet_type_odds_band") or {}).get(bet_type) or {}
    info = cross.get(band)
    if info and info.get("n", 0) >= 20 and info.get("factor") is not None:
        factor = float(info["factor"])
    if factor is None:
        info = (purchase_factors.get("by_odds_band") or {}).get(band)
        if info and info.get("n", 0) >= 50 and info.get("factor") is not None:
            factor = float(info["factor"])
    bt_info = (purchase_factors.get("by_bet_type") or {}).get(bet_type)
    bt_factor = None
    if bt_info and bt_info.get("n", 0) >= 30 and bt_info.get("factor") is not None:
        bt_factor = float(bt_info["factor"])
        if factor is None:
            factor = bt_factor
    if factor is None:
        overall = purchase_factors.get("overall") or {}
        if overall.get("factor") is not None and (overall.get("n") or 0) >= 100:
            factor = float(overall["factor"])

    if factor is None:
        return est_prob

    # 券種が明確に悪い/良いときは券種係数で上限を掛ける
    if bt_factor is not None:
        factor = min(factor, bt_factor)

    factor = max(0.08, min(1.5, factor))
    return max(0.0, min(1.0, float(est_prob) * factor))




def _apply_high_odds_residual(est_prob: float, odds_value: float, bet_type: str, high_odds_factors: dict) -> float:
    """
    1000-3000倍・3000倍以上帯の的中率残差を掛ける(方針B)。
    券種×帯があれば優先、なければ帯全体。係数が無ければそのまま。
    """
    if not high_odds_factors or est_prob is None or est_prob <= 0:
        return est_prob
    try:
        odds = float(odds_value) if odds_value is not None else 0.0
    except (TypeError, ValueError):
        return est_prob
    band = _odds_band_key(odds)
    if band not in ("1000-3000倍", "3000倍以上"):
        return est_prob

    factor = None
    cross = (high_odds_factors.get("by_bet_type_odds_band") or {}).get(bet_type) or {}
    info = cross.get(band)
    if info and info.get("factor") is not None:
        factor = float(info["factor"])
    if factor is None:
        info = (high_odds_factors.get("by_odds_band") or {}).get(band)
        if info and info.get("factor") is not None:
            factor = float(info["factor"])
    if factor is None:
        return est_prob
    factor = max(0.08, min(1.5, factor))
    return max(0.0, min(1.0, float(est_prob) * factor))


def _save_skipped_bets(db: Session, race_id: int, skipped_for_verification: list) -> int:
    """
    見送った買い目を「見送り」として記録する。
    戻り値: 新規に保存した件数。
    """
    if not skipped_for_verification:
        return 0
    existing_skipped_keys = {
        (s.bet_type, s.combination)
        for s in db.query(models.SkippedBet).filter(models.SkippedBet.race_id == race_id).all()
    }
    new_skipped_objs = []
    seen_in_this_call = set()
    for c, reason in skipped_for_verification:
        key = (c["bet_type"], c["combination"])
        if key in existing_skipped_keys or key in seen_in_this_call:
            continue
        seen_in_this_call.add(key)
        # all_evaluated 由来は win_prob、candidates 由来も win_prob
        wp = c.get("win_prob")
        if wp is None and c.get("estimated_win_prob_pct") is not None:
            wp = float(c["estimated_win_prob_pct"]) / 100.0
        new_skipped_objs.append(models.SkippedBet(
            race_id=race_id,
            bet_type=c["bet_type"],
            combination=c["combination"],
            win_prob_estimated=wp,
            win_prob_raw=c.get("win_prob_raw"),
            ev_pct_estimated=c.get("ev_pct"),
            reason=reason,
        ))
    if new_skipped_objs:
        db.add_all(new_skipped_objs)
        db.commit()
    return len(new_skipped_objs)



def _select_portfolio(
    candidates,
    race_cap,
    max_items,
    avoid_garami,
    outcomes,
    odds_safety_margins,
):
    """固定ケリー額を使い、ガラミ制約を満たす期待利益最大のポートフォリオを構成する。"""
    prepared = []

    # 候補ごとの払戻対象結果を事前計算する。
    # 選択ループ内で judge_purchase_result を繰り返さない。
    winning_cache = {}

    for c in candidates:
        stake = calc.round_to_bet_unit(c["raw_stake"])
        if stake <= 0:
            continue

        key = (c["bet_type"], c["combination"])
        if key not in winning_cache:
            winning_cache[key] = [
                o
                for o in outcomes
                if calc.judge_purchase_result(
                    c["bet_type"],
                    c["combination"],
                    list(o),
                )
            ]

        prepared.append({
            **c,
            "_stake": stake,
            "_value": stake * (c["win_prob"] * c["odds_value"] - 1.0),
            "_winning": winning_cache[key],
        })

    limit = max_items if max_items and max_items > 0 else len(prepared)

    selected = []
    payout = {o: 0.0 for o in outcomes}
    total_stake = 0.0
    remaining = list(prepared)
    # ガラミ制約が無効の場合は、投票額を100円単位の重みとして、
    # 予算内・件数上限内で期待利益合計を最大化する。
    if not avoid_garami:
        unit = 100
        cap_units = int(race_cap // unit)

        # 同一投票額では期待利益が最大の候補だけ残す。
        best_by_weight = {}
        for c in prepared:
            weight = int(c["_stake"] // unit)
            if weight <= 0 or weight > cap_units:
                continue
            current = best_by_weight.get(weight)
            if current is None or c["_value"] > current["_value"]:
                best_by_weight[weight] = c

        compressed = list(best_by_weight.values())

        # 0/1ナップサックDP。
        # 選択候補の履歴をtupleではなくPython整数のビットマスクで保持する。
        # 目的関数・予算制約・件数制約・降順更新は従来と同一。
        neg_inf = float("-inf")
        dp = [
            [(neg_inf, 0) for _ in range(cap_units + 1)]
            for _ in range(limit + 1)
        ]
        dp[0][0] = (0.0, 0)

        for i, c in enumerate(compressed):
            weight = int(c["_stake"] // unit)
            value = c["_value"]
            bit = 1 << i
            max_count = min(limit, i + 1)

            # 降順更新により同一候補の重複選択を防止。
            for count in range(max_count, 0, -1):
                prev = dp[count - 1]
                current = dp[count]

                for used in range(cap_units, weight - 1, -1):
                    prev_profit, prev_mask = prev[used - weight]
                    if prev_profit == neg_inf:
                        continue

                    candidate_profit = prev_profit + value
                    current_profit, _ = current[used]

                    if candidate_profit > current_profit:
                        current[used] = (
                            candidate_profit,
                            prev_mask | bit,
                        )

        best_profit = 0.0
        best_mask = 0

        for count in range(1, limit + 1):
            for used in range(cap_units + 1):
                profit, mask = dp[count][used]
                if profit > best_profit:
                    best_profit = profit
                    best_mask = mask

        selected = [
            c
            for i, c in enumerate(compressed)
            if best_mask & (1 << i)
        ]

        total_stake = sum(
            c["_stake"]
            for c in selected
        )

        for c in selected:
            for outcome in c["_winning"]:
                payout[outcome] += (
                    c["_stake"]
                    * c["odds_value"]
                )

        return selected, payout, 0, len(prepared)

    def can_add(candidate):
        if not avoid_garami:
            return True

        new_total_stake = total_stake + candidate["_stake"]
        margin_pct = odds_safety_margins.get(
            candidate["bet_type"],
            purchases_router.DEFAULT_ODDS_SAFETY_MARGIN_PCT,
        )
        payout_gain = (
            candidate["_stake"]
            * candidate["odds_value"]
            * (1 - margin_pct / 100)
        )

        winning = candidate["_winning"]

        # 新候補が的中する結果について、追加後もガラミにならないか確認。
        for outcome in winning:
            if payout[outcome] + payout_gain < new_total_stake:
                return False

        # 既存の払戻対象結果についても、追加投資によって
        # 払戻額 < 総投資額にならないことを確認。
        for outcome, current_payout in payout.items():
            if current_payout > 0 and outcome not in winning:
                if current_payout < new_total_stake:
                    return False

        return True

    while remaining and len(selected) < limit:
        best = None
        best_value = float("-inf")

        for c in remaining:
            if total_stake + c["_stake"] > race_cap:
                continue
            if not can_add(c):
                continue

            if c["_value"] > best_value:
                best = c
                best_value = c["_value"]

        if best is None:
            break

        selected.append(best)
        remaining.remove(best)

        margin_pct = odds_safety_margins.get(
            best["bet_type"],
            purchases_router.DEFAULT_ODDS_SAFETY_MARGIN_PCT,
        )
        payout_gain = (
            best["_stake"]
            * best["odds_value"]
            * (1 - margin_pct / 100)
        )

        for outcome in best["_winning"]:
            payout[outcome] += payout_gain

        total_stake += best["_stake"]

    if avoid_garami:
        selected_keys = {
            (c["bet_type"], c["combination"])
            for c in selected
        }

        for c in prepared:
            if (c["bet_type"], c["combination"]) in selected_keys:
                continue
            if total_stake + c["_stake"] > race_cap:
                continue
            if not can_add(c):
                rejected_garami += 1

    return selected, payout, rejected_garami, len(prepared)

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

    calibration_factors = purchases_router.get_calibration_factors_retroactive(db)

    created = []
    low_prob_warnings = {}
    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob_raw = _estimate_prob(win_probs, o.bet_type, cars, line_map=line_map, line_boost=line_boost)
        if getattr(req, "apply_calibration", True):
            est_prob, low_prob_warning, _, _ = _apply_calibration(est_prob_raw, calibration_factors, bet_type=o.bet_type)
        else:
            est_prob, low_prob_warning = est_prob_raw, False
        low_prob_warnings[(o.bet_type, o.combination)] = low_prob_warning

        market_prob = normalized_market.get(o.bet_type, {}).get(
            o.combination, calc.market_prob_from_odds(o.odds_value, o.bet_type)
        )

        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value, req.rebate_pct)
        is_skip, skip_reason = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)

        stake_info = calc.recommend_stake(
            bankroll, est_prob, o.odds_value, req.fractional_coefficient, req.max_bet_pct_per_bet, req.rebate_pct
        )
        # 100円単位に切り捨てた結果0円(=理論上の賭け金が最低単位に満たない)になった場合は、
        # 期待値がプラスでも実質的に賭けようがないため見送り扱いにする(のんの指摘により変更)。
        if not is_skip and stake_info["recommended_stake"] == 0:
            is_skip = True
            skip_reason = "理論上の賭け金が最低単位(100円)に満たないため見送り"
        # 100円ベット換算で、安全マージン(オッズ変動対策)を考慮した閾値以上を「買い示唆」とする
        is_recommended = (not is_skip) and (ev_pct >= req.min_ev_pct)

        ev_result = models.EvResult(
            race_id=race_id,
            bet_type=o.bet_type,
            combination=o.combination,
            estimated_win_prob=est_prob,
            estimated_win_prob_raw=est_prob_raw,
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
    calibration_factors = purchases_router.get_calibration_factors_retroactive(db)

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
            est_prob, low_prob_warning, _, _ = _apply_calibration(est_prob, calibration_factors, bet_type=bet_type)
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

    # のんの報告「race-planが1レース約45〜50秒かかる」の原因調査用の区間計測。
    # 校正キャッシュは効いていることが確認済み(process_pid同一)のため、
    # どのフェーズが実際に時間を食っているかをここで直接計測する。
    _t0 = _time.time()

    entries = db.query(models.Entry).filter(models.Entry.race_id == race_id).all()
    win_probs = _build_win_probs(entries)
    if not win_probs:
        raise HTTPException(400, "選手の勝率データが揃っていません")

    bankroll = req.bankroll if req.bankroll is not None else bankroll_router.get_current_balance(db)
    odds_rows = db.query(models.Odds).filter(models.Odds.race_id == race_id).all()
    _t1 = _time.time()  # ここまで: レース・出走表・オッズ取得
    if not odds_rows:
        # 以前は400エラーで止めていたが、再予想バッチ処理中にオッズ無しレースが
        # 混ざっていると、そこでバッチ全体がエラー扱いになってしまっていた。
        # オッズが無いのはこのレース固有の事情(取得漏れ等)であり、買い示唆が
        # 出せないだけなので、正常応答としてスキップ可能な形にした
        # (のんの実機運用で判明した問題を受けて修正)。
        return {
            "race_id": race_id,
            "message": "オッズデータがないため、このレースは投票プランを作成できません(スキップ)",
            "items": [],
            "total_stake": 0,
            "skipped_no_odds": True,
        }

    candidates = []
    # 買い示唆に至ったかどうかに関わらず、評価した組み合わせを全て保持しておく
    # (下で見送り記録に使うため)。
    all_evaluated = []
    calibration_factors = purchases_router.get_calibration_factors_retroactive(db)
    # 実購入集合で残る楽観バイアス用の追加係数（選別後校正）
    purchase_set_factors = purchases_router.get_purchase_set_calibration_factors(db)
    high_odds_residual_factors = purchases_router.get_high_odds_residual_factors(db)
    _t2 = _time.time()  # ここまで: 校正係数(第1段+第2段)取得

    # 着順まで当てる必要がある券種(3連単・2車単)は、顔ぶれだけ当てればいい券種
    # (3連複・2車複・ワイド)より難しく、レースのステージ(S級決勝等)によっては
    # 着順の読みが極端に外れやすいことが確認されている(のんの実機運用での検証結果)。
    # そのステージでの結果確定済みレース数がまだ少ないうちは、着順指定の券種だけ
    # 見送り、顔ぶれ判定の券種は通常通り投票対象にする。件数が閾値を超えたら
    # 自動的に通常運用へ戻る(のんの要望により追加)。
    ORDER_SENSITIVE_BET_TYPES = {"3連単", "2車単"}
    MIN_STAGE_SAMPLE_FOR_ORDER_BETS = 30
    stage_sample_n = None
    if race.race_stage:
        stage_sample_n = (
            db.query(models.Race)
            .filter(models.Race.race_stage == race.race_stage)
            .filter(models.Race.actual_result.isnot(None))
            .count()
        )
    stage_sample_insufficient = (
        race.race_stage is not None and stage_sample_n is not None
        and stage_sample_n < MIN_STAGE_SAMPLE_FOR_ORDER_BETS
    )
    _t3 = _time.time()  # ここまで: stage_sample_nのcount()クエリ

    stage_gated_keys = set()
    performance_gated_keys = set()  # (bet_type, combination) -> reason
    apply_gates = getattr(req, "apply_performance_gates", True)

    # 実績に基づくステージのゲート用データ(検証経路では使わない)
    stage_exp_map = {}
    if apply_gates:
        try:
            stage_exp_map = purchases_router.get_stage_expectancy_map(db, min_samples=50)
        except Exception:
            stage_exp_map = {}

    # 実績に基づく券種のゲート用データ。
    # 「どの券種を選んでも、個々の券種で期待値プラスでなければならない」という
    # のんの方針を受けて追加(ステージゲートと同じ考え方・同じ閾値を券種にも適用)。
    # ステージゲートが「その時期のそのステージの読みが甘い」を検出するのに対し、
    # こちらは「その券種自体の予想ロジックが継続的に実績で負けている」ことを検出する。
    # 両者は独立していて、どちらか一方に該当すれば見送りになる。
    bet_type_exp_map = {}
    bet_type_odds_band_exp_map = {}
    if apply_gates:
        try:
            bet_type_exp_map = purchases_router.get_bet_type_expectancy_map(db, min_samples=50)
        except Exception:
            bet_type_exp_map = {}
        try:
            bet_type_odds_band_exp_map = purchases_router.get_bet_type_odds_band_expectancy_map(
                db, min_samples=30
            )
        except Exception:
            bet_type_odds_band_exp_map = {}
    _t4 = _time.time()  # ここまで: ステージ/券種ゲート集計取得(キャッシュ済みのはず)

    # 実績ゲートは「確率を捨てる」のではなく、不調ステージのみ見送り(券種差別なし)。
    # マルチ専用の確率縮小・最低勝率・券種除外は行わない。
    STAGE_EXPECTANCY_CUTOFF = -50.0
    # 券種ゲート: 実績収支がマイナス（ROI<100%）の券種は見送り。
    # 2車単など継続赤字の券種をプランから外す。
    BET_TYPE_EXPECTANCY_CUTOFF = 0.0

    # ライン構成を買い目確率に反映
    line_map, line_boost = _line_map_from_race(race)

    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob_raw = _estimate_prob(win_probs, o.bet_type, cars, line_map=line_map, line_boost=line_boost)
        if getattr(req, "apply_calibration", True):
            est_prob, low_prob_warning, data_sufficiency_pct, accuracy_pct = _apply_calibration(
                est_prob_raw, calibration_factors, bet_type=o.bet_type
            )
            # 第2段: 購入集合で観測された券種×オッズ帯の残差を掛ける
            if getattr(req, "apply_purchase_set_calibration", True):
                est_prob = _apply_purchase_set_factor(
                    est_prob, o.odds_value, o.bet_type, purchase_set_factors
                )
                # 方針B: 高オッズ帯は的中率残差でさらに確率を寄せる（禁止ではない）
                if getattr(req, "apply_purchase_set_calibration", True):
                    est_prob = _apply_high_odds_residual(
                        est_prob, o.odds_value, o.bet_type, high_odds_residual_factors
                    )
                low_prob_warning = est_prob < 0.05
        else:
            est_prob, low_prob_warning, data_sufficiency_pct, accuracy_pct = est_prob_raw, False, 0.0, None
        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value, req.rebate_pct)
        is_skip, _ = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)

        effective_min_ev = req.min_ev_pct
        gate_reason = None
        if apply_gates:
            # 不調ステージのみ除外(券種は問わない)
            if race.race_stage and race.race_stage in stage_exp_map:
                st = stage_exp_map[race.race_stage]
                if st["expectancy_pct"] < STAGE_EXPECTANCY_CUTOFF:
                    gate_reason = (
                        f"不調ステージ除外({race.race_stage}:実績{st['expectancy_pct']}%/"
                        f"{st['n']}件)"
                    )
            # 不調券種を除外(全期間の実績収支率ベース。ステージゲートとは独立で、
            # どちらか一方に該当すれば見送りになる)。
            if gate_reason is None and o.bet_type in bet_type_exp_map:
                bt_exp = bet_type_exp_map[o.bet_type]
                if bt_exp["expectancy_pct"] < BET_TYPE_EXPECTANCY_CUTOFF:
                    gate_reason = (
                        f"不調券種除外({o.bet_type}:実績{bt_exp['expectancy_pct']}%/"
                        f"{bt_exp['n']}件)"
                    )
            # 券種×オッズ帯の実績ゲート（例: 3連単は全体黒字でも1000-3000倍だけ赤字なら除外）
            if gate_reason is None and bet_type_odds_band_exp_map:
                band = _odds_band_key(float(o.odds_value) if o.odds_value else 0)
                key = f"{o.bet_type}|{band}"
                cell = bet_type_odds_band_exp_map.get(key)
                if cell and cell.get("expectancy_pct") is not None:
                    if cell["expectancy_pct"] < BET_TYPE_EXPECTANCY_CUTOFF:
                        gate_reason = (
                            f"不調券種×オッズ帯除外({o.bet_type}/{band}:"
                            f"実績{cell['expectancy_pct']}%/{cell['n']}件)"
                        )
            # 購入集合で的中が極端に不足している券種も見送り
            # （収支ゲートをすり抜けても、的中比が壊滅的なら買わない）
            if gate_reason is None and purchase_set_factors:
                ps_bt = (purchase_set_factors.get("by_bet_type") or {}).get(o.bet_type)
                if (
                    ps_bt
                    and (ps_bt.get("n") or 0) >= 80
                    and ps_bt.get("factor") is not None
                    and float(ps_bt["factor"]) <= 0.25
                ):
                    gate_reason = (
                        f"購入集合で的中不足({o.bet_type}:係数{ps_bt['factor']}/"
                        f"{ps_bt['n']}件)"
                    )

        is_recommended = (not is_skip) and (ev_pct >= effective_min_ev) and (gate_reason is None)
        stage_order_gate = apply_gates and stage_sample_insufficient and o.bet_type in ORDER_SENSITIVE_BET_TYPES
        if stage_order_gate:
            is_recommended = False
            stage_gated_keys.add((o.bet_type, o.combination))
        if gate_reason and (not is_skip) and (ev_pct >= req.min_ev_pct):
            # EV閾値は超えていたが実績ゲートで落ちたものだけ記録
            performance_gated_keys.add((o.bet_type, o.combination, gate_reason))

        all_evaluated.append({
            "bet_type": o.bet_type,
            "combination": o.combination,
            "win_prob": est_prob,
            "win_prob_raw": est_prob_raw,
            "ev_pct": round(ev_pct, 2),
            "gate_reason": gate_reason,
            "effective_min_ev": effective_min_ev,
        })
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
            "win_prob_raw": est_prob_raw,
            "total_vote_amount": o.total_vote_amount,
            "low_prob_warning": low_prob_warning,
            # データ充足度%(=その勝率帯の補正がどれだけ実績データに裏付けられているか)と、
            # 予想精度%(=予想確率と実績的中率がどれだけ一致しているか)。
            # どちらも表示専用の指標であり、確率計算・フィルタリング・投票内容には使わない
            # (のんの要望により追加、のんの指摘により2指標に分離)。
            "data_sufficiency_pct": data_sufficiency_pct,
            "prediction_accuracy_pct": accuracy_pct,
            "effective_min_ev": effective_min_ev,
        })

    # 買い示唆にすら至らなかった組み合わせ(最低勝率フィルターや期待値の閾値で
    # 弾かれたもの)も、大穴帯を中心に大量データを集めて検証したいため、全て見送り
    # 記録の対象にする(のんの要望により追加)。
    # 以前は「そのレースで買い示唆が1件も無かった場合」にしか記録されておらず、
    # 他に買い示唆があるレースでは、そもそも候補に入らなかった大穴帯等の組み合わせが
    # 記録から漏れていた。投票プラン自体の絞り込み(表示・購入対象)は変更せず、
    # 検証用データの収集だけを目的とした追加。
    #
    # 【重要な追加(のんの指摘により修正)】
    # 従来は期待値(ev_pct)がマイナスの組み合わせを一律で検証対象から除外していた。
    # しかしこれにより、「的中したがAIの計算では期待値マイナスだった買い目」が
    # SkippedBetに一切記録されず、bet-type-diagnosticsの捕捉率診断で
    # 「候補生成漏れ」として誤って計上される原因になっていた(実際にはAIが評価は
    # していたのに、期待値マイナスという理由で記録だけ捨てていた)。
    # かといって全件記録するとNeonの容量制限に再度当たるリスクが高いため、
    # 券種ごとに推定確率(win_prob)が高い順の上位N件だけは、期待値がマイナスでも
    # 検証用に残す。実際に勝つ組み合わせの大半は確率上位に来るはずなので、
    # DB容量を抑えつつ検証の見落としを大きく減らせる。
    NEGATIVE_EV_VERIFICATION_TOP_N = 30  # 除外群ROI検証のため枠を拡大(のんの診断要望)
    _t5 = _time.time()  # ここまで: 候補評価ループ(for o in odds_rows)本体
    negative_ev_by_type = {}
    for e in all_evaluated:
        if e["ev_pct"] <= 0:
            negative_ev_by_type.setdefault(e["bet_type"], []).append(e)
    negative_ev_keep_keys = set()
    for bt, items in negative_ev_by_type.items():
        top_items = sorted(items, key=lambda x: x["win_prob"], reverse=True)[:NEGATIVE_EV_VERIFICATION_TOP_N]
        for item in top_items:
            negative_ev_keep_keys.add((item["bet_type"], item["combination"]))

    recommended_keys = {(c["bet_type"], c["combination"]) for c in candidates}
    skipped_for_verification = []  # (candidate, reason) 後でSkippedBetとして記録する
    for e in all_evaluated:
        if e["ev_pct"] <= 0 and (e["bet_type"], e["combination"]) not in negative_ev_keep_keys:
            continue  # 期待値マイナス、かつ券種内の確率上位N件にも入らないものは検証対象にしない
        if (e["bet_type"], e["combination"]) not in recommended_keys:
            if e["ev_pct"] <= 0:
                skipped_for_verification.append((
                    e,
                    f"期待値マイナス(確率上位{NEGATIVE_EV_VERIFICATION_TOP_N}件のため検証用に記録)",
                ))
            elif (e["bet_type"], e["combination"]) in stage_gated_keys:
                skipped_for_verification.append((
                    e,
                    f"このステージの検証データ不足({stage_sample_n}/{MIN_STAGE_SAMPLE_FOR_ORDER_BETS}件)のため着順指定の券種を見送り",
                ))
            elif e.get("gate_reason"):
                skipped_for_verification.append((e, e["gate_reason"]))
            else:
                # 実績ゲートでEV底上げされた場合
                eff = e.get("effective_min_ev")
                if eff is not None and e.get("ev_pct", 0) >= req.min_ev_pct and e.get("ev_pct", 0) < eff:
                    skipped_for_verification.append((
                        e,
                        f"実績に基づくEV閾値未達(必要{eff}%/実際{e.get('ev_pct')}%)",
                    ))
                else:
                    skipped_for_verification.append((e, "買い示唆なし(EV/確率が閾値未満)"))

    excluded_low_prob_count = 0
    if req.exclude_low_prob_warning:
        before_count = len(candidates)
        kept = []
        for c in candidates:
            if c["low_prob_warning"]:
                skipped_for_verification.append((c, "大穴帯(未補正)のため除外"))
            else:
                kept.append(c)
        candidates = kept
        excluded_low_prob_count = before_count - len(candidates)

    if not candidates:
        n_skip = _save_skipped_bets(db, race_id, skipped_for_verification)
        return {
            "race_id": race_id,
            "message": "安全マージンを満たす買い示唆がありませんでした(見送り推奨)",
            "items": [],
            "total_stake": 0,
            "skipped_saved_count": n_skip,
            "skipped_candidate_count": len(skipped_for_verification),
        }

    race_cap = bankroll * req.max_race_pct

    # --- ガミり(的中はしたのに合計投票額を下回る)を避けるための「起こりうる結果」の洗い出し ---
    # 券種をまたいで買い目を選ぶと、同じ結果で複数券が同時的中したり、逆に1つしか
    # 的中しなかったりする。3連単の着順(上位3着の並び)がどの券種の的中・不的中も
    # 一意に決めるため、これを「起こりうる結果」の全体集合として使う。
    car_numbers = sorted({e.car_number for e in entries})
    outcomes = list(itertools.permutations(car_numbers, 3)) if len(car_numbers) >= 3 else []
    # 各結果(起こりうる着順)の確率。3連単の的中確率と全く同じ計算(Harville式)。
    outcome_probs = {o: _estimate_prob(win_probs, "3連単", o, line_map=line_map, line_boost=line_boost) for o in outcomes} if outcomes else {}

    # 投票時オッズは締切までにズレる(券種によってズレ幅が大きく異なり、実測でワイドは
    # 3連単の4倍近くズレることが分かっている)。ガミり判定はこのズレを見込んで、
    # 実際のオッズより保守的な値で計算し、多少ズレても崩れない安全マージンを持たせる。
    odds_safety_margins = (
        purchases_router.get_odds_safety_margins(db)
        if getattr(req, "apply_odds_safety_margin", True)
        else {}
    )

    def _winning_outcomes(bet_type: str, cars: tuple) -> list:
        combination_str = "-".join(str(c) for c in cars)
        return [o for o in outcomes if calc.judge_purchase_result(bet_type, combination_str, list(o))]

    selected, payout_by_outcome, excluded_by_garami_count, prepared_count = _select_portfolio(
        candidates=candidates,
        race_cap=race_cap,
        max_items=req.max_items,
        avoid_garami=req.avoid_garami,
        outcomes=outcomes,
        odds_safety_margins=odds_safety_margins,
    )

    items = []
    total_stake = 0.0
    total_expected_profit = 0.0
    selected_keys = set()

    for c in selected:
        stake = c["_stake"]
        selected_keys.add((c["bet_type"], c["combination"]))
        expected_profit = stake * (c["win_prob"] * c["odds_value"] - 1.0)
        total_stake += stake
        total_expected_profit += expected_profit

        total_vote = c["total_vote_amount"]
        self_impact_pct = (
            round(stake / (total_vote + stake) * 100, 2)
            if total_vote is not None and total_vote > 0 else None
        )
        items.append({
            "bet_type": c["bet_type"],
            "combination": c["combination"],
            "estimated_win_prob_pct": c["estimated_win_prob_pct"],
            "win_prob": c["win_prob"],
            "win_prob_raw": c.get("win_prob_raw"),
            "odds_value": c["odds_value"],
            "ev_pct": c["ev_pct"],
            "roi_pct": round(c["ev_pct"] + 100, 2),
            "stake": stake,
            "self_impact_pct": self_impact_pct,
            "self_impact_warning": self_impact_pct is not None and self_impact_pct >= 2.0,
            "low_prob_warning": c["low_prob_warning"],
            "data_sufficiency_pct": c["data_sufficiency_pct"],
            "prediction_accuracy_pct": c["prediction_accuracy_pct"],
        })

    # ポートフォリオ選択で採用されなかった買い示唆は、検証用に見送りとして残す。
    for c in candidates:
        key = (c["bet_type"], c["combination"])
        if key not in selected_keys:
            skipped_for_verification.append((c, "ポートフォリオ最適化で選外"))

    excluded_by_budget_count = max(0, prepared_count - len(selected) - excluded_by_garami_count)
    excluded_by_min_stake_count = len(candidates) - prepared_count

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

    _t6 = _time.time()  # ここまで: skipped_for_verification組み立て・ポートフォリオ選定等
    n_skip = _save_skipped_bets(db, race_id, skipped_for_verification)
    _t7 = _time.time()  # ここまで: SkippedBet保存(db.add_all + commit)

    _timings = {
        "fetch_race_entries_odds": round(_t1 - _t0, 2),
        "calibration_factors": round(_t2 - _t1, 2),
        "stage_sample_count_query": round(_t3 - _t2, 2),
        "gate_expectancy_maps": round(_t4 - _t3, 2),
        "candidate_eval_loop": round(_t5 - _t4, 2),
        "portfolio_and_verification_build": round(_t6 - _t5, 2),
        "save_skipped_bets": round(_t7 - _t6, 2),
        "total": round(_t7 - _t0, 2),
        "odds_rows_count": len(odds_rows),
        "all_evaluated_count": len(all_evaluated),
        "skipped_candidate_count": len(skipped_for_verification),
    }

    return {
        "race_id": race_id,
        "num_bets": len(items),
        "total_stake": round(total_stake, 0),
        "skipped_saved_count": n_skip,
        "skipped_candidate_count": len(skipped_for_verification),
        "race_budget_cap": round(race_cap, 0),
        "excluded_by_budget_count": excluded_by_budget_count,
        "excluded_by_garami_count": excluded_by_garami_count,
        "excluded_by_min_stake_count": excluded_by_min_stake_count,
        "debug_timings": _timings,
        "excluded_low_prob_count": excluded_low_prob_count,
        # 実際にどの設定でこのプランが作られたかを明示する(除外されたはずの大穴帯が
        # 混ざっていた場合など、原因切り分けをしやすくするため。のんの報告により追加)。
        "exclude_low_prob_warning_requested": req.exclude_low_prob_warning,
        "performance_gates_applied": apply_gates,
        "performance_gated_count": len(performance_gated_keys),
        "stage_expectancy_used": stage_exp_map.get(race.race_stage) if race.race_stage else None,
        "bet_type_expectancy_used": bet_type_exp_map,
        "bet_type_odds_band_expectancy_count": len(bet_type_odds_band_exp_map or {}),
        "bet_type_odds_band_expectancy_used": bet_type_odds_band_exp_map,
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
