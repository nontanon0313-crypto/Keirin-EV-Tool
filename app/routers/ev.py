import itertools
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import models, schemas
from .. import ev_calculator as calc

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

    created = []
    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        ordered = o.bet_type in calc.ORDERED_BET_TYPES
        est_prob = calc.combination_prob(win_probs, cars, ordered)

        market_prob = normalized_market.get(o.bet_type, {}).get(
            o.combination, calc.market_prob_from_odds(o.odds_value, o.bet_type)
        )

        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value)
        is_skip, skip_reason = calc.apply_min_prob_filter(est_prob, ev_pct, req.min_win_prob)
        # 100円ベット換算で期待値1円以上(=EV% 1%以上)を「買い示唆」とする
        is_recommended = (not is_skip) and (ev_pct >= 1.0)

        stake_info = calc.recommend_stake(
            req.bankroll, est_prob, o.odds_value, req.fractional_coefficient, req.max_bet_pct_per_bet
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

    # 買い示唆を最優先、その中で期待値が高い順に並べる。次に見送りを末尾に回す。
    results = sorted(
        created,
        key=lambda r: (not r.is_recommended, r.is_skip, -r.ev_pct),
    )

    return {
        "race_id": race_id,
        "bankroll": req.bankroll,
        "total_combinations": len(results),
        "results": [
            {
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
            }
            for r in results
        ],
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
        est_prob = calc.combination_prob(win_probs, combo, ordered)
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
