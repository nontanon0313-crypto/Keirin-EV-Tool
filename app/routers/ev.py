import itertools
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


def _save_skipped_bets(db: Session, race_id: int, skipped_for_verification: list) -> None:
    """
    見送った買い目を「見送り」として記録する(のんの指摘により追加)。
    以前はSkippedBetテーブル・APIが存在するのに一度も自動記録されておらず、
    「除外して正解だったか、本当は当たっていたのに逃したのか」を検証できていなかった。
    同一の(race_id, bet_type, combination)を二重記録しないようにする。
    """
    if not skipped_for_verification:
        return
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
        new_skipped_objs.append(models.SkippedBet(
            race_id=race_id,
            bet_type=c["bet_type"],
            combination=c["combination"],
            win_prob_estimated=c["win_prob"],
            win_prob_raw=c.get("win_prob_raw"),
            ev_pct_estimated=c["ev_pct"],
            reason=reason,
        ))
    if new_skipped_objs:
        db.add_all(new_skipped_objs)
        db.commit()


def _apply_calibration(est_prob: float, calibration_factors: dict) -> tuple:
    """
    購入実績に基づく自動補正係数を適用する。
    以前は「試行数が十分(200÷帯の代表確率)になるまで補正なし」だったが、
    大穴帯(必要数8000件等)は事実上ずっと補正されないままになるため、
    サンプル数に応じて段階的に補正を効かせる方式に変更した(のんの要望により修正)。
    calibration_factorsの各帯には、この段階的補正が既に反映されている。
    戻り値: (補正後確率, その勝率帯の低確率帯かつ実績未成熟=推定誤差に注意すべきか,
             データ充足度%, 予想精度%)
    データ充足度% = その勝率帯の試行数 ÷ 必要試行数(100%上限)。「どれだけ実績データに
    裏付けられた補正か」を示す指標。
    予想精度% = 予想確率と実績的中率の一致度。「予想がどれだけ当たっているか」を示す指標。
    以前はこの2つを混同して「予想精度%」という1つの名前でデータ充足度の方を表示していた
    (のんの指摘により分離)。どちらも表示専用で、確率計算・フィルタリング・投票内容には
    一切使わない(のんの要望により追加)。
    """
    bucket_name, _ = calc.get_prob_bucket(est_prob)
    info = calibration_factors.get(bucket_name)
    is_reliable = bool(info and info["is_reliable"])
    # オッズが跳ねる低確率帯(0-5%=大穴)は、確率推定のわずかな誤差がEVの計算上
    # 大きく増幅されるため、実績が十分溜まって確信が持てるまでは特に注意が必要
    # (この警告フラグ自体は段階的補正とは別に、閾値到達までは出し続ける)。
    low_prob_warning = (bucket_name == "0-5%(大穴)") and not is_reliable
    data_sufficiency_pct = 0.0
    if info and info["required_sample_count"] > 0:
        data_sufficiency_pct = round(min(1.0, info["sample_count"] / info["required_sample_count"]) * 100, 1)
    accuracy_pct = info["prediction_accuracy_pct"] if info else None
    if not info:
        return est_prob, low_prob_warning, data_sufficiency_pct, accuracy_pct
    calibrated = est_prob * info["calibration_factor"]
    return max(0.0, min(1.0, calibrated)), low_prob_warning, data_sufficiency_pct, accuracy_pct


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
        est_prob_raw = _estimate_prob(win_probs, o.bet_type, cars)
        if getattr(req, "apply_calibration", True):
            est_prob, low_prob_warning, _, _ = _apply_calibration(est_prob_raw, calibration_factors)
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
            est_prob, low_prob_warning, _, _ = _apply_calibration(est_prob, calibration_factors)
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
    calibration_factors = purchases_router.get_calibration_factors(db)

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

    stage_gated_keys = set()
    performance_gated_keys = set()  # (bet_type, combination) -> reason
    apply_gates = getattr(req, "apply_performance_gates", True)

    # 実績に基づく券種・ステージのゲート用データ(検証経路では使わない)
    stage_exp_map = {}
    bet_exp_map = {}
    if apply_gates:
        try:
            stage_exp_map = purchases_router.get_stage_expectancy_map(db, min_samples=50)
            bet_exp_map = purchases_router.get_bet_type_expectancy_map(db, min_samples=50)
        except Exception:
            stage_exp_map = {}
            bet_exp_map = {}

    # 券種ごとの追加EV閾値(実績で不調な券種はより高いEVを要求)
    # 3連単は利益の柱のため追加なし。他は想定が楽観的なため底上げする。
    BET_TYPE_EV_BONUS = {
        "3連単": 0.0,
        "3連複": 10.0,
        "ワイド": 15.0,
        "2車複": 15.0,
        "2車単": 20.0,
    }
    # 本命帯(30%以上)は的中率が想定の約半分のため追加閾値
    FAVORITE_BAND_EV_BONUS = 10.0
    # ステージ実績がこの値未満なら、そのステージの買い目を実投票から除外
    STAGE_EXPECTANCY_CUTOFF = -50.0
    # 券種実績がこの値未満なら、その券種は実投票から除外(3連単は対象外)
    BET_TYPE_EXPECTANCY_CUTOFF = -40.0
    # マルチ(3連単以外): 想定的中が実績の約2倍 → 確率を実績比で縮小し的中率を改善
    MULTI_PROB_SCALE = {
        "3連複": 0.50,
        "ワイド": 0.50,
        "2車複": 0.55,
        "2車単": 0.45,
    }
    MULTI_MIN_WIN_PROB = {
        "3連複": 0.08,
        "ワイド": 0.12,
        "2車複": 0.08,
        "2車単": 0.06,
    }
    BET_TYPE_HARD_EXCLUDE = {"2車単", "2車複"}

    for o in odds_rows:
        cars = tuple(int(x) for x in o.combination.split("-"))
        est_prob_raw = _estimate_prob(win_probs, o.bet_type, cars)
        if getattr(req, "apply_calibration", True):
            est_prob, low_prob_warning, data_sufficiency_pct, accuracy_pct = _apply_calibration(est_prob_raw, calibration_factors)
        else:
            est_prob, low_prob_warning, data_sufficiency_pct, accuracy_pct = est_prob_raw, False, 0.0, None

        multi_scale = 1.0
        if apply_gates and o.bet_type in MULTI_PROB_SCALE:
            multi_scale = MULTI_PROB_SCALE[o.bet_type]
            est_prob = max(0.0, min(1.0, est_prob * multi_scale))

        ev_pct = calc.calc_ev_pct(est_prob, o.odds_value, req.rebate_pct)

        effective_min_prob = req.min_win_prob
        if apply_gates and o.bet_type in MULTI_MIN_WIN_PROB:
            effective_min_prob = max(effective_min_prob, MULTI_MIN_WIN_PROB[o.bet_type])
        is_skip, _ = calc.apply_min_prob_filter(est_prob, ev_pct, effective_min_prob)

        effective_min_ev = req.min_ev_pct
        gate_reason = None
        if apply_gates:
            effective_min_ev += BET_TYPE_EV_BONUS.get(o.bet_type, 5.0)
            if est_prob >= 0.30:
                effective_min_ev += FAVORITE_BAND_EV_BONUS
            if race.race_stage and race.race_stage in stage_exp_map:
                st = stage_exp_map[race.race_stage]
                if st["expectancy_pct"] < STAGE_EXPECTANCY_CUTOFF:
                    gate_reason = (
                        f"不調ステージ除外({race.race_stage}:実績{st['expectancy_pct']}%/"
                        f"{st['n']}件)"
                    )
            if gate_reason is None and o.bet_type in BET_TYPE_HARD_EXCLUDE:
                gate_reason = f"不調券種除外({o.bet_type}:実投票では見送り)"
            elif gate_reason is None and o.bet_type != "3連単" and o.bet_type in bet_exp_map:
                bt = bet_exp_map[o.bet_type]
                if (
                    bt["expectancy_pct"] < BET_TYPE_EXPECTANCY_CUTOFF
                    and o.bet_type not in MULTI_PROB_SCALE
                ):
                    gate_reason = (
                        f"不調券種除外({o.bet_type}:実績{bt['expectancy_pct']}%/"
                        f"{bt['n']}件)"
                    )
            if gate_reason is None and is_skip and o.bet_type in MULTI_MIN_WIN_PROB:
                gate_reason = (
                    f"マルチ最低勝率未達({o.bet_type}:必要{effective_min_prob*100:.0f}%/"
                    f"補正後{est_prob*100:.1f}%)"
                )

        is_recommended = (not is_skip) and (ev_pct >= effective_min_ev) and (gate_reason is None)
        stage_order_gate = stage_sample_insufficient and o.bet_type in ORDER_SENSITIVE_BET_TYPES
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
        # 不調寄り券種は賭け金も抑制(3連単以外)
        stake_mult = 1.0
        if apply_gates and o.bet_type != "3連単":
            stake_mult = 0.5
        raw_stake = bankroll * f_capped * stake_mult
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
            "stake_mult": stake_mult,
            "effective_min_ev": effective_min_ev,
        })

    # 買い示唆にすら至らなかった組み合わせ(最低勝率フィルターや期待値の閾値で
    # 弾かれたもの)も、大穴帯を中心に大量データを集めて検証したいため、全て見送り
    # 記録の対象にする(のんの要望により追加)。
    # 以前は「そのレースで買い示唆が1件も無かった場合」にしか記録されておらず、
    # 他に買い示唆があるレースでは、そもそも候補に入らなかった大穴帯等の組み合わせが
    # 記録から漏れていた。投票プラン自体の絞り込み(表示・購入対象)は変更せず、
    # 検証用データの収集だけを目的とした追加。
    recommended_keys = {(c["bet_type"], c["combination"]) for c in candidates}
    skipped_for_verification = []  # (candidate, reason) 後でSkippedBetとして記録する
    for e in all_evaluated:
        if e["ev_pct"] <= 0:
            continue  # 期待値マイナスは見送って当然のため検証対象にしない
        if (e["bet_type"], e["combination"]) not in recommended_keys:
            if (e["bet_type"], e["combination"]) in stage_gated_keys:
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
        _save_skipped_bets(db, race_id, skipped_for_verification)
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
    excluded_by_min_stake_count = 0

    for c in sorted(candidates, key=lambda x: -x["ev_pct"]):
        if req.max_items and len(items) >= req.max_items:
            skipped_for_verification.append((c, "最大件数の都合で除外"))
            continue
        stake = calc.round_to_bet_unit(c["raw_stake"])
        if stake <= 0:
            # 理論上の賭け金が最低単位(100円)に満たない(切り捨てで0円になった)
            excluded_by_min_stake_count += 1
            skipped_for_verification.append((c, "理論上の賭け金が最低単位未満"))
            continue
        if stake > remaining_budget:
            if remaining_budget < 100:
                # もう1点(最低100円)も買う余地が無いので、以降は全て予算オーバー
                skipped_for_verification.append((c, "予算超過"))
                break
            stake = calc.round_to_bet_unit(remaining_budget)
            if stake <= 0 or stake > remaining_budget:
                excluded_by_budget_count += 1
                skipped_for_verification.append((c, "予算超過"))
                continue

        cars = tuple(int(x) for x in c["combination"].split("-"))
        winning = _winning_outcomes(c["bet_type"], cars) if req.avoid_garami else []

        if req.avoid_garami:
            if not winning:
                # 起こりうる結果と1つも一致しない(データ不整合)場合は安全側でスキップ
                excluded_by_garami_count += 1
                skipped_for_verification.append((c, "データ不整合のため除外"))
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
                skipped_for_verification.append((c, "ガミり回避のため除外"))
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

    _save_skipped_bets(db, race_id, skipped_for_verification)

    return {
        "race_id": race_id,
        "num_bets": len(items),
        "total_stake": round(total_stake, 0),
        "race_budget_cap": round(race_cap, 0),
        "excluded_by_budget_count": excluded_by_budget_count,
        "excluded_by_garami_count": excluded_by_garami_count,
        "excluded_by_min_stake_count": excluded_by_min_stake_count,
        "excluded_low_prob_count": excluded_low_prob_count,
        # 実際にどの設定でこのプランが作られたかを明示する(除外されたはずの大穴帯が
        # 混ざっていた場合など、原因切り分けをしやすくするため。のんの報告により追加)。
        "exclude_low_prob_warning_requested": req.exclude_low_prob_warning,
        "performance_gates_applied": apply_gates,
        "performance_gated_count": len(performance_gated_keys),
        "stage_expectancy_used": stage_exp_map.get(race.race_stage) if race.race_stage else None,
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
