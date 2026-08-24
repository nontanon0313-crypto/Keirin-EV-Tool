from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from typing import Optional

from ..database import get_db
from .. import models, schemas
from . import bankroll as bankroll_router
from .. import ev_calculator as calc

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.post("/")
def create_purchase(purchase: schemas.PurchaseCreate, db: Session = Depends(get_db)):
    obj = models.Purchase(**purchase.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    # 購入した分だけ証拠金残高を減算する
    bankroll_router.adjust_balance(db, -purchase.stake_amount)
    return obj


@router.post("/bulk")
def create_purchases_bulk(payload: schemas.PurchaseBulkCreate, db: Session = Depends(get_db)):
    """
    証拠金プランの結果などをまとめて購入記録する。
    以前はフロント側で1件ずつ/purchases/を呼んでいたため、件数が多いと
    (通信+DB commitが件数分×2回発生し)極端に時間がかかり、画面遷移で
    途中のFetchが切れる不具合もあった。1回のリクエスト・1回のcommitで済ませる。
    """
    if not payload.items:
        return {"created_count": 0, "purchase_ids": []}

    objs = [models.Purchase(**item.model_dump()) for item in payload.items]
    db.add_all(objs)
    db.commit()
    for obj in objs:
        db.refresh(obj)

    total_stake = sum(item.stake_amount for item in payload.items)
    bankroll_router.adjust_balance(db, -total_stake)

    return {"created_count": len(objs), "purchase_ids": [o.id for o in objs]}


@router.put("/{purchase_id}/result")
def update_purchase_result(purchase_id: int, update: schemas.PurchaseResultUpdate, db: Session = Depends(get_db)):
    obj = db.query(models.Purchase).get(purchase_id)
    if not obj:
        raise HTTPException(404, "購入履歴が見つかりません")
    if obj.result != "pending":
        raise HTTPException(400, "この購入履歴はすでに結果が確定しています(二重加算を防ぐため再更新できません)")
    obj.result = update.result
    obj.payout_amount = update.payout_amount
    obj.final_odds = update.final_odds
    db.commit()
    # 払戻があれば証拠金残高に加算する(負けの場合はpayout_amount=0なので変化なし)
    bankroll_router.adjust_balance(db, update.payout_amount)
    return obj


def get_calibration_factors(db: Session) -> dict:
    """
    勝率帯ごとの自動補正係数を計算する。
    試行数が「200÷帯の代表確率」に達した帯だけ、実績に基づく補正係数を返す(段階的補正)。

    重要: Purchase(実際に買った分)だけでなく、SkippedBet(大穴帯除外等で見送った分、
    結果確定済みのもの)も合わせて集計する。以前はPurchaseしか見ておらず、
    大穴帯は「投票から除外され続ける限りデータも永遠に貯まらない」状態になっていた。
    見送った買い目も「予想確率 vs 実際の結果」というデータとしては全く同じ形なので、
    的中検証・自動補正には활用できる(のんの指摘により修正。投票対象からの除外と、
    集計・検証対象からの除外は別問題)。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    skipped = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None)).all()

    # Purchase/SkippedBetを「予想確率・的中したか・情報源」という共通の形に正規化して結合する
    records = [
        (p.win_prob_at_purchase, p.result == "win", "purchase")
        for p in purchases if p.win_prob_at_purchase is not None
    ]
    records += [
        (s.win_prob_estimated, s.actual_result == "win", "skipped")
        for s in skipped if s.win_prob_estimated is not None
    ]

    result = {}
    for lo, hi, name, mid in calc.PROB_BUCKETS:
        bucket_records = [(prob, won, src) for prob, won, src in records if lo <= prob < hi]
        count = len(bucket_records)
        purchase_count = sum(1 for _, _, src in bucket_records if src == "purchase")
        skipped_count = count - purchase_count
        required = calc.required_sample_size(mid)
        is_reliable = count >= required

        if count > 0:
            wins = sum(1 for _, won, _ in bucket_records if won)
            actual_win_rate = wins / count
            predicted_avg = sum(prob for prob, _, _ in bucket_records) / count
        else:
            wins = 0
            actual_win_rate = None
            predicted_avg = None

        deviation_pct = None
        significance_p_value = None
        significance_p_value_pct = None
        if actual_win_rate is not None and predicted_avg is not None:
            # 実績的中率 - 予想平均確率。プラス=予想が実際より低め(過小評価)、
            # マイナス=予想が実際より高め(過大評価)だったことを意味する。
            deviation_pct = round((actual_win_rate - predicted_avg) * 100, 2)
            # このズレが単なる偶然のブレなのか、統計的に有意なのかを二項検定で判定する
            # (のんの指摘により追加)。小さいほど「偶然では説明しにくい」。
            significance_p_value = calc.binomial_lower_tail_p(wins, count, predicted_avg)
            significance_p_value_pct = round(significance_p_value * 100, 4)

        # 以前は「必要数に達するまで補正係数1.0」のon/off切り替えだったが、
        # これだと大穴帯(必要数8000件等)は事実上永遠に補正されない。
        # サンプル数に応じて段階的に補正を効かせる方式に変更。
        # さらに、統計的証拠(p値)が強い場合はサンプル数不足でも早めに補正を
        # 効かせるようにした(のんの実機検証=組み合わせ確率の系統的過大評価を受けて追加)。
        factor = 1.0
        if count > 0 and predicted_avg:
            raw_factor = calc.compute_calibration_factor(actual_win_rate, predicted_avg)
            factor = calc.shrunk_calibration_factor(raw_factor, count, required, p_value=significance_p_value)

        # 「予想精度%」= 予想確率と実績的中率の一致度(データ充足度とは別物)。
        # is_reliable/required_sample_countは「どれだけ実績データに裏付けられているか」であり、
        # こちらは「予想が実際どれだけ当たっているか」を表す(のんの指摘により追加)。
        accuracy_pct = calc.prediction_accuracy_pct(actual_win_rate, predicted_avg)

        result[name] = {
            "sample_count": count,
            "purchase_count": purchase_count,
            "skipped_count": skipped_count,
            "required_sample_count": required,
            "is_reliable": is_reliable,
            "actual_win_rate_pct": round(actual_win_rate * 100, 2) if actual_win_rate is not None else None,
            "predicted_avg_prob_pct": round(predicted_avg * 100, 2) if predicted_avg is not None else None,
            "deviation_pct": deviation_pct,
            "significance_p_value_pct": significance_p_value_pct,
            "calibration_factor": round(factor, 3),
            "prediction_accuracy_pct": accuracy_pct,
        }
    return result


DEFAULT_ODDS_SAFETY_MARGIN_PCT = 20.0


def get_odds_safety_margins(db: Session) -> dict:
    """
    券種ごとに「投票時オッズ→最終オッズ」の実績ズレ(最悪ケース)から、
    ガミり回避チェックに使う安全マージンを算出する。
    券種によってズレの大きさが大きく異なる(のんの実測: 3連単は約1割、ワイドは約4割)ため、
    全体で1つの値にせず券種別に持つ。実績が少ない券種はデフォルト値を使う。
    """
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.final_odds.isnot(None), models.Purchase.odds_at_purchase.isnot(None))
        .all()
    )
    min_sample = 5
    by_bet_type = {}
    for p in purchases:
        drift = (p.final_odds - p.odds_at_purchase) / p.odds_at_purchase * 100
        by_bet_type.setdefault(p.bet_type, []).append(drift)

    margins = {}
    for bt, drifts in by_bet_type.items():
        if len(drifts) < min_sample:
            continue
        worst = min(drifts)  # オッズが最も下がった(不利側に動いた)実績
        margins[bt] = max(DEFAULT_ODDS_SAFETY_MARGIN_PCT, abs(worst)) if worst < 0 else DEFAULT_ODDS_SAFETY_MARGIN_PCT
    return margins


@router.get("/source-weights")
def source_weights(db: Session = Depends(get_db)):
    """
    tipstarの勝率とAI推定、どちらが実際に精度が高いかを、着順確定済みレースの
    実績(Brierスコア: 予測確率と実際の結果の二乗誤差、低いほど精度が高い)から算出する。
    サンプルが少ない場合はデフォルトの1:1のまま。
    """
    min_races_for_trust = 5

    races = db.query(models.Race).filter(models.Race.actual_result.isnot(None)).all()

    app_sq_error_sum = 0.0
    ai_sq_error_sum = 0.0
    sample_count = 0

    for race in races:
        try:
            winner_car = int(race.actual_result.split("-")[0])
        except (ValueError, IndexError):
            continue

        entries = db.query(models.Entry).filter(models.Entry.race_id == race.id).all()
        app_probs = {e.car_number: e.app_win_rate for e in entries if e.app_win_rate is not None}
        ai_probs = {e.car_number: e.ai_win_prob for e in entries if e.ai_win_prob is not None}

        if not app_probs or not ai_probs:
            continue

        # レース内で正規化(tipstar値は%表記、AI推定は既に0-1の確率として保存されている)
        app_total = sum(app_probs.values())
        ai_total = sum(ai_probs.values())
        if app_total <= 0 or ai_total <= 0:
            continue

        for car in set(app_probs) & set(ai_probs):
            outcome = 1.0 if car == winner_car else 0.0
            app_p = app_probs[car] / app_total
            ai_p = ai_probs[car] / ai_total
            app_sq_error_sum += (app_p - outcome) ** 2
            ai_sq_error_sum += (ai_p - outcome) ** 2
            sample_count += 1

    if len(races) < min_races_for_trust or sample_count == 0:
        return {
            "app_weight": 0.5,
            "ai_weight": 0.5,
            "based_on_actual_data": False,
            "resolved_race_count": len(races),
            "reason": f"着順確定済みレースが{len(races)}件({min_races_for_trust}件以上で自動算出に切り替わります)。デフォルトの1:1のままです。",
        }

    app_brier = app_sq_error_sum / sample_count
    ai_brier = ai_sq_error_sum / sample_count

    # Brierスコアは低いほど精度が高いため、逆数を重みにする(0除算を避けるための下駄を履かせる)
    app_inv = 1.0 / max(app_brier, 0.001)
    ai_inv = 1.0 / max(ai_brier, 0.001)
    total_inv = app_inv + ai_inv
    app_weight = app_inv / total_inv
    ai_weight = ai_inv / total_inv

    return {
        "app_weight": round(app_weight, 3),
        "ai_weight": round(ai_weight, 3),
        "based_on_actual_data": True,
        "resolved_race_count": len(races),
        "sample_count": sample_count,
        "app_brier_score": round(app_brier, 4),
        "ai_brier_score": round(ai_brier, 4),
        "reason": f"着順確定済み{len(races)}レース分の実績から算出しました(値が低いほど精度が高いBrierスコア: tipstar={round(app_brier,4)} / AI={round(ai_brier,4)})。",
    }


@router.get("/investment-readiness")
def investment_readiness(db: Session = Depends(get_db)):
    """
    「実資金を投資してよいか」を、具体的な数値基準で自動判定する
    (のんの要望により追加)。
    基準:
    1. サンプル数が十分か(統計的な結論を出すのに足る量か)
    2. 予想と実績のズレが統計的に有意でないか(偶然の範囲に収まっているか)
    3. 実績収支率が黒字か、レース単位でも安定してプラスが多いか
    4. 実績の勝率・オッズで運用した場合、破産確率が十分低いか
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    if not purchases:
        return {"ready": False, "message": "まだ確定した購入履歴がありません。"}

    n_bets = len(purchases)
    race_ids = sorted({p.race_id for p in purchases})
    n_races = len(race_ids)

    win_prob_values = [p.win_prob_at_purchase for p in purchases if p.win_prob_at_purchase is not None]
    win_count = sum(1 for p in purchases if p.result == "win")
    total_stake = sum(p.stake_amount for p in purchases)
    total_payout = sum(p.payout_amount for p in purchases)
    overall_roi_pct = round((total_payout / total_stake) * 100, 2) if total_stake else 0

    race_profit_flags = []
    for rid in race_ids:
        race_purchases = [p for p in purchases if p.race_id == rid]
        race_stake = sum(p.stake_amount for p in race_purchases)
        race_payout = sum(p.payout_amount for p in race_purchases)
        race_profit_flags.append(1 if race_payout >= race_stake else 0)
    race_profit_rate_pct = round(sum(race_profit_flags) / n_races * 100, 1) if n_races else 0

    p_value_pct = None
    if win_prob_values:
        avg_predicted = sum(win_prob_values) / len(win_prob_values)
        p_value_pct = round(calc.binomial_lower_tail_p(win_count, len(win_prob_values), avg_predicted) * 100, 2)

    odds_purchases = [p for p in purchases if p.odds_at_purchase is not None]
    avg_odds = (
        sum(p.stake_amount * p.odds_at_purchase for p in odds_purchases) / sum(p.stake_amount for p in odds_purchases)
        if odds_purchases else None
    )
    win_rate = win_count / n_bets if n_bets else 0

    bankruptcy = None
    if avg_odds and win_rate > 0:
        bankruptcy = calc.monte_carlo_bankruptcy(
            initial_bankroll=100000, win_prob=win_rate, odds_value=avg_odds,
            stake_fraction=0.10 / max(1, round(n_bets / max(1, n_races))),
            num_bets_per_trial=n_bets, num_trials=3000, ruin_threshold_pct=0.5,
        )

    # 基準ごとの判定(のんの基準: サンプル十分・ズレが偶然の範囲・黒字が安定・破産しない)
    checks = {
        "sample_size": {
            "pass": n_bets >= 200 and n_races >= 30,
            "detail": f"購入{n_bets}件・{n_races}レース(目安: 200件以上・30レース以上)",
        },
        "calibration": {
            "pass": p_value_pct is not None and p_value_pct >= 20,
            "detail": f"偶然に起きる確率{p_value_pct}%(目安: 20%以上で偶然の範囲内)" if p_value_pct is not None else "データ不足",
        },
        "profitability": {
            "pass": overall_roi_pct >= 100 and race_profit_rate_pct >= 40,
            "detail": f"実績収支率{overall_roi_pct}% / レース黒字率{race_profit_rate_pct}%(目安: 収支率100%以上・黒字率40%以上)",
        },
        "bankruptcy_risk": {
            "pass": bankruptcy is not None and bankruptcy["ruin_probability_pct"] <= 10,
            "detail": (
                f"実績ベースの破産確率{bankruptcy['ruin_probability_pct']}%(目安: 10%以下)"
                if bankruptcy else "データ不足(平均オッズまたは的中実績が無い)"
            ),
        },
    }
    all_pass = all(c["pass"] for c in checks.values())

    return {
        "ready": all_pass,
        "summary": "投資を始める目安を満たしています" if all_pass else "まだ目安を満たしていません(下記の未達項目を確認)",
        "checks": checks,
        "n_bets": n_bets,
        "n_races": n_races,
    }


@router.get("/suggested-margin")
def suggested_margin(db: Session = Depends(get_db)):
    """
    実績のオッズ変動(投票時→最終オッズのズレ)から、安全マージンの目安を自動算出する。
    データが少ない場合はデフォルト値(5%)を返す。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    drift_info = _odds_drift_stats(purchases)

    default_margin = 5.0
    min_sample_for_trust = 10

    if drift_info.get("message") or drift_info.get("sample_count", 0) < min_sample_for_trust:
        return {
            "suggested_margin_pct": default_margin,
            "based_on_actual_data": False,
            "reason": f"実績データが不足しています({drift_info.get('sample_count', 0)}件、{min_sample_for_trust}件以上で自動算出に切り替わります)。デフォルト値を使用してください。",
        }

    avg_drift = drift_info["avg_odds_drift_pct"]
    # 不利方向(オッズが下がる)のブレ幅をそのまま安全マージンとして使う。有利方向のブレならデフォルト値を維持。
    if avg_drift < 0:
        suggested = max(default_margin, abs(avg_drift))
    else:
        suggested = default_margin

    return {
        "suggested_margin_pct": round(suggested, 1),
        "based_on_actual_data": True,
        "sample_count": drift_info["sample_count"],
        "avg_odds_drift_pct": avg_drift,
        "reason": f"実績{drift_info['sample_count']}件のオッズ変動(平均{avg_drift}%)から算出しました。",
    }


@router.get("/calibration")
def calibration_status(db: Session = Depends(get_db)):
    """勝率帯ごとの自動補正の状態(補正係数・信頼できるか・必要試行数)と、全体1本のズレも返す。"""
    buckets = get_calibration_factors(db)

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending", models.Purchase.win_prob_at_purchase.isnot(None))
        .all()
    )
    overall = None
    if purchases:
        wins = sum(1 for p in purchases if p.result == "win")
        actual = wins / len(purchases)
        predicted = sum(p.win_prob_at_purchase for p in purchases) / len(purchases)
        p_value = calc.binomial_lower_tail_p(wins, len(purchases), predicted)
        overall = {
            "sample_count": len(purchases),
            "actual_win_rate_pct": round(actual * 100, 2),
            "predicted_avg_prob_pct": round(predicted * 100, 2),
            "deviation_pct": round((actual - predicted) * 100, 2),
            "significance_p_value_pct": round(p_value * 100, 4),
        }

    return {"overall": overall, "buckets": buckets}


@router.delete("/{purchase_id}")
def delete_purchase(purchase_id: int, db: Session = Depends(get_db)):
    """
    購入履歴を1件削除する。実際には投票しなかった(見送った)未確定の記録の削除に加え、
    バグ等による重複登録の後始末のため、確定済み(win/lose)の記録も削除できるようにしている
    (のんの要望により変更。以前は確定済みは削除不可だったが、重複データを消せず
    検証結果が歪んだままになる問題があった)。
    注意: 証拠金残高はこの削除で自動調整されない(購入時の減算・払戻時の加算を遡って
    取り消す処理はしていない)。証拠金に影響がある場合は、証拠金タブから手動で調整してください。
    """
    obj = db.query(models.Purchase).get(purchase_id)
    if not obj:
        raise HTTPException(404, "購入履歴が見つかりません")
    db.delete(obj)
    db.commit()
    return {"deleted": True, "purchase_id": purchase_id}


@router.delete("/pending/by-race/{race_id}")
def delete_pending_purchases_by_race(race_id: int, db: Session = Depends(get_db)):
    """指定レースの未確定(pending)購入履歴をまとめて削除する。実際には投票しなかった分の整理用。"""
    deleted_count = (
        db.query(models.Purchase)
        .filter(models.Purchase.race_id == race_id, models.Purchase.result == "pending")
        .delete()
    )
    db.commit()
    return {"deleted_count": deleted_count, "race_id": race_id}


@router.get("/big-expected-bets")
def big_expected_bets(db: Session = Depends(get_db), limit: int = 20):
    """
    想定利益(投資額 × 想定期待値)が大きい順に購入履歴を並べる。
    「想定損益の合計は大きいのに実績が伸びない」場合、少数の高額期待値の買い目が
    的中/不的中でどれだけ結果を左右しているかを確認するための一覧
    (のんの要望により追加)。
    """
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.ev_pct_at_purchase.isnot(None))
        .all()
    )
    race_ids = {p.race_id for p in purchases}
    races_by_id = (
        {r.id: r for r in db.query(models.Race).filter(models.Race.id.in_(race_ids)).all()}
        if race_ids else {}
    )

    items = []
    for p in purchases:
        expected_profit = p.stake_amount * p.ev_pct_at_purchase / 100
        race = races_by_id.get(p.race_id)
        items.append({
            "purchase_id": p.id,
            "race_id": p.race_id,
            "venue_name": race.venue_name if race else "不明",
            "race_number": race.race_number if race else None,
            "bet_type": p.bet_type,
            "combination": p.combination,
            "stake_amount": p.stake_amount,
            "odds_at_purchase": p.odds_at_purchase,
            "win_prob_at_purchase_pct": (
                round(p.win_prob_at_purchase * 100, 2) if p.win_prob_at_purchase is not None else None
            ),
            "ev_pct_at_purchase": p.ev_pct_at_purchase,
            "expected_profit": round(expected_profit, 0),
            "result": p.result,
            "payout_amount": p.payout_amount,
            "actual_profit": (
                round(p.payout_amount - p.stake_amount, 0) if p.result != "pending" else None
            ),
        })
    items.sort(key=lambda x: -x["expected_profit"])
    return items[:limit]


@router.get("/car-pick-accuracy")
def car_pick_accuracy(db: Session = Depends(get_db)):
    """
    券種の組み合わせによるノイズを除き、「そのレースでAIが最有力とした車番」が
    実際に1着/上位3着に来たかどうかだけを追跡する。
    同一レース内の複数買い目が、実質同じ車番予想を券種違いで何度も張っているだけ
    (相関が強く、独立試行として扱えない)という問題を避けた、より純粋な予測精度の指標。
    「1レース=1試行」なので、二項検定もそのまま正しく使える(のんの指摘により追加)。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )
    items = []
    for race in races:
        entries = [e for e in race.entries if e.blended_win_prob is not None]
        if not entries:
            continue
        top_pick = max(entries, key=lambda e: e.blended_win_prob)
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except (ValueError, IndexError):
            continue
        if not parsed["groups"]:
            continue
        first_group = parsed["groups"][0]
        items.append({
            "race_id": race.id,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
            "top_pick_car_number": top_pick.car_number,
            "top_pick_player_name": top_pick.player_name,
            "predicted_win_prob_pct": round(top_pick.blended_win_prob * 100, 2),
            "actual_result": race.actual_result,
            "won": top_pick.car_number in first_group,
            "in_top3": top_pick.car_number in parsed["top3_set"],
        })

    if not items:
        return {"message": "着順確定済み・AI推定済みのレースがまだありません"}

    n = len(items)
    win_count = sum(1 for it in items if it["won"])
    top3_count = sum(1 for it in items if it["in_top3"])
    avg_predicted_pct = sum(it["predicted_win_prob_pct"] for it in items) / n
    p_value = calc.binomial_lower_tail_p(win_count, n, avg_predicted_pct / 100)

    if p_value < 0.05:
        judgement = "予想が実態より高すぎる可能性が高い(偶然では説明しにくい)"
    elif p_value < 0.20:
        judgement = "やや予想が高めだが、まだ偶然の範囲とも言える"
    else:
        judgement = "現時点のサンプル数では、偶然のブレの範囲内"

    return {
        "n_races": n,
        "win_count": win_count,
        "win_rate_pct": round(win_count / n * 100, 1),
        "top3_count": top3_count,
        "top3_rate_pct": round(top3_count / n * 100, 1),
        "avg_predicted_win_prob_pct": round(avg_predicted_pct, 2),
        "significance_p_value_pct": round(p_value * 100, 4),
        "judgement": judgement,
        "items": sorted(items, key=lambda x: -x["race_id"]),
    }


@router.get("/pending")
def list_pending_purchases(db: Session = Depends(get_db)):
    """まだ結果未確定の購入履歴一覧(結果入力画面用)。レースごとにまとめられるよう、レース情報も付与する。"""
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result == "pending")
        .order_by(models.Purchase.purchased_at.desc())
        .all()
    )
    race_ids = {p.race_id for p in purchases}
    races_by_id = {
        r.id: r for r in db.query(models.Race).filter(models.Race.id.in_(race_ids)).all()
    } if race_ids else {}
    result = []
    for p in purchases:
        race = races_by_id.get(p.race_id)
        result.append({
            "id": p.id,
            "race_id": p.race_id,
            "venue_name": race.venue_name if race else "不明",
            "race_number": race.race_number if race else None,
            "bet_type": p.bet_type,
            "combination": p.combination,
            "stake_amount": p.stake_amount,
            "odds_at_purchase": p.odds_at_purchase,
        })
    return result


@router.get("/races-awaiting-result")
def list_races_awaiting_result(db: Session = Depends(get_db), limit: int = 30):
    """
    結果(actual_result)がまだ記録されていないレースの一覧。
    購入(Purchase)が0件のレース(=買い示唆なしで見送ったレース)も含む。

    以前は「未確定の購入を読み込む」画面がPurchaseテーブルだけを見ており、
    買い目が1件も無かったレースはこの一覧に出てこなかった。結果として、
    投票しなかったレースの結果を記録する入り口が無く、検証データ
    (的中率検証・キャリブレーション)が蓄積できなかった
    (のんの指摘により追加)。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.is_(None))
        .order_by(models.Race.race_date.desc().nullslast(), models.Race.id.desc())
        .limit(limit)
        .all()
    )
    race_ids = [r.id for r in races]
    purchase_counts = {}
    if race_ids:
        rows = (
            db.query(models.Purchase.race_id, func.count(models.Purchase.id))
            .filter(models.Purchase.race_id.in_(race_ids))
            .group_by(models.Purchase.race_id)
            .all()
        )
        purchase_counts = {race_id: count for race_id, count in rows}
    return [
        {
            "race_id": r.id,
            "venue_name": r.venue_name,
            "race_number": r.race_number,
            "race_date": r.race_date,
            "event_title": r.event_title,
            "purchase_count": purchase_counts.get(r.id, 0),
        }
        for r in races
    ]


@router.get("/")
def list_purchases(race_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(models.Purchase)
    if race_id:
        q = q.filter(models.Purchase.race_id == race_id)
    return q.order_by(models.Purchase.purchased_at.desc()).all()


@router.get("/stats")
def purchase_stats(db: Session = Depends(get_db)):
    """
    勝率帯別・券種別の回収率など、複数の切り口で集計する。
    単一要素だけで結論づけないためのFX版ルールを踏襲。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    if not purchases:
        return {"message": "まだ確定した購入履歴がありません"}

    total_stake = sum(p.stake_amount for p in purchases)
    total_payout = sum(p.payout_amount for p in purchases)
    overall_expectancy_pct = ((total_payout - total_stake) / total_stake * 100) if total_stake else 0

    # 以前は切り口(バンク別・季節別...)ごとに、購入1件ごとDBへ都度Race/Entryを問い合わせて
    # いたため、購入件数が増えるほど(切り口数×購入件数のクエリが飛ぶため)極端に遅くなっていた。
    # ここで対象レースのRace/Entryを1回だけ一括取得してキャッシュし、以降は辞書参照にする。
    race_ids = {p.race_id for p in purchases}
    races_by_id = {
        r.id: r
        for r in db.query(models.Race)
        .options(joinedload(models.Race.bank))
        .filter(models.Race.id.in_(race_ids))
        .all()
    }
    entries_by_race_id = {}
    if race_ids:
        for e in db.query(models.Entry).filter(models.Entry.race_id.in_(race_ids)).all():
            entries_by_race_id.setdefault(e.race_id, []).append(e)

    # 「1番人気オッズ集中度パターン」判定用: レースごとの3連単最低オッズ(1番人気)を取得する。
    # 境界値は遠山競輪研究所(Gamboo)の実データ分析(S級・A12班9車立て基準)を採用。
    # https://gamboo.jp/topics/?tid=tohyama024-pc (のんが共有)
    top_fav_odds_by_race = {}
    if race_ids:
        tan_odds = (
            db.query(models.Odds)
            .filter(models.Odds.race_id.in_(race_ids), models.Odds.bet_type == "3連単")
            .all()
        )
        for o in tan_odds:
            cur = top_fav_odds_by_race.get(o.race_id)
            if cur is None or o.odds_value < cur:
                top_fav_odds_by_race[o.race_id] = o.odds_value

    def popularity_pattern_bucket(p):
        odds = top_fav_odds_by_race.get(p.race_id)
        if odds is None:
            return "3連単オッズなし"
        if odds <= 5.6:
            return "超人気集中型(1番人気〜5.6倍)"
        if odds <= 9.9:
            return "人気集中型(5.7〜9.9倍)"
        if odds <= 17.7:
            return "標準型(10.0〜17.7倍)"
        return "人気分散型(17.8倍〜)"

    def bucket_stats(key_fn):
        buckets = {}
        for p in purchases:
            key = key_fn(p)
            b = buckets.setdefault(key, {
                "stake": 0.0, "payout": 0.0, "count": 0, "wins": 0,
                "win_prob_sum": 0.0, "win_prob_count": 0,
                "ev_profit_sum": 0.0, "ev_stake_sum": 0.0,
            })
            b["stake"] += p.stake_amount
            b["payout"] += p.payout_amount
            b["count"] += 1
            if p.result == "win":
                b["wins"] += 1
            # win_prob_at_purchase / ev_pct_at_purchase は、購入時点でのAI予想値
            # (実績ではなく「買った時、AIはどう見積もっていたか」のスナップショット)。
            if p.win_prob_at_purchase is not None:
                b["win_prob_sum"] += p.win_prob_at_purchase
                b["win_prob_count"] += 1
            # 投資額で加重平均する(証拠金の日々の変動で1点あたりの投資額が違うため、
            # 単純平均だと少額の買い目と高額の買い目が同じ重みになってしまう)。
            if p.ev_pct_at_purchase is not None:
                b["ev_profit_sum"] += p.stake_amount * p.ev_pct_at_purchase / 100
                b["ev_stake_sum"] += p.stake_amount
        out = {}
        for k, v in buckets.items():
            expectancy = ((v["payout"] - v["stake"]) / v["stake"] * 100) if v["stake"] else 0
            expected_win_rate_pct = (
                round(v["win_prob_sum"] / v["win_prob_count"] * 100, 1) if v["win_prob_count"] else None
            )
            # ev_pct_at_purchaseは「0%が損益分岐点」表現のため、+100して実績(roi_pct)と
            # 同じ「100%が損益分岐点」表現に揃える。さらに投資額で加重する
            # (外部監査・のんの指摘により修正)。
            expected_roi_pct = (
                round((v["ev_profit_sum"] / v["ev_stake_sum"] + 1) * 100, 2) if v["ev_stake_sum"] else None
            )
            expected_profit = round(v["ev_profit_sum"], 0) if v["ev_stake_sum"] else None
            out[k] = {
                "count": v["count"],
                "win_rate_pct": round(v["wins"] / v["count"] * 100, 1),
                "expected_win_rate_pct": expected_win_rate_pct,
                # roi_pct: 回収率(100%が損益分岐点)。expectancy_pct: 同じ値を「0%が損益分岐点」の表現にしたもの。
                "roi_pct": round(expectancy + 100, 2),
                "expectancy_pct": round(expectancy, 2),
                "profit": round(v["payout"] - v["stake"], 0),
                "expected_roi_pct": expected_roi_pct,
                "expected_profit": expected_profit,
            }
        # 実績が高い順に並べ替える
        return dict(sorted(out.items(), key=lambda item: -item[1]["expectancy_pct"]))

    def prob_bucket(p):
        prob = p.win_prob_at_purchase or 0
        name, _ = calc.get_prob_bucket(prob)
        return name

    def line_bucket(p):
        race = races_by_id.get(p.race_id)
        if not race or not race.lines_data:
            return "ライン情報なし"
        line_map = {}
        for idx, line in enumerate(race.lines_data):
            for car in line:
                line_map[int(car)] = idx
        try:
            cars = [int(x) for x in p.combination.split("-")]
        except ValueError:
            return "ライン情報なし"
        line_ids = [line_map.get(c) for c in cars]
        if any(lid is None for lid in line_ids):
            return "ライン情報なし"
        return "同ライン絡み" if len(set(line_ids)) == 1 else "異なるライン混在"

    def bank_lead_bucket(p):
        race = races_by_id.get(p.race_id)
        if not race or not race.bank or race.bank.lead_advantage_score is None:
            return "バンク情報なし"
        score = race.bank.lead_advantage_score
        if score >= 0.66:
            return "先行有利バンク(直線短め)"
        elif score >= 0.33:
            return "標準的なバンク"
        else:
            return "差し有利バンク(直線長め)"

    def race_stage_bucket(p):
        race = races_by_id.get(p.race_id)
        if not race or not race.race_stage:
            return "不明"
        return race.race_stage

    def season_bucket(p):
        race = races_by_id.get(p.race_id)
        if not race or not race.season:
            return "不明"
        return race.season

    def grade_bucket(p):
        race = races_by_id.get(p.race_id)
        if not race or not race.grade:
            return "不明"
        return race.grade

    def bank_bucket(p):
        # 旧実装はp.tags["bank"]を参照していたが、tagsはどこからも書き込まれておらず
        # 常に「不明」になっていた不具合があったため、race.venue_nameから直接取得するよう修正。
        race = races_by_id.get(p.race_id)
        if not race or not race.venue_name:
            return "不明"
        return race.venue_name

    def _combination_cars(p):
        try:
            return [int(x) for x in p.combination.split("-")]
        except (ValueError, AttributeError):
            return []

    # 競走得点帯の区切り(A級〜S級上位までを想定した目安の帯。データが増えたら見直す)
    RACE_SCORE_BUCKETS = [
        (0, 55, "55未満"),
        (55, 65, "55-65"),
        (65, 75, "65-75"),
        (75, 10 ** 6, "75以上"),
    ]

    def race_score_bucket(p):
        cars = _combination_cars(p)
        if not cars:
            return "得点情報なし"
        entries = [e for e in entries_by_race_id.get(p.race_id, []) if e.car_number in cars]
        scores = [e.race_score for e in entries if e.race_score is not None]
        if not scores:
            return "得点情報なし"
        avg_score = sum(scores) / len(scores)
        for lo, hi, name in RACE_SCORE_BUCKETS:
            if lo <= avg_score < hi:
                return f"買い目内平均得点:{name}"
        return "買い目内平均得点:75以上"

    def leg_style_bucket(p):
        cars = _combination_cars(p)
        if not cars:
            return "脚質情報なし"
        entries = [e for e in entries_by_race_id.get(p.race_id, []) if e.car_number in cars]
        styles = [e.leg_style for e in entries if e.leg_style]
        if not styles:
            return "脚質情報なし"
        unique_styles = set(styles)
        if len(unique_styles) == 1:
            return f"買い目内脚質:{unique_styles.pop()}のみ"
        return "買い目内脚質:混在(" + "・".join(sorted(unique_styles)) + ")"

    all_buckets = {
        "券種別": bucket_stats(lambda p: p.bet_type),
        "勝率帯別": bucket_stats(prob_bucket),
        "バンク別": bucket_stats(bank_bucket),
        "ライン絡み別": bucket_stats(line_bucket),
        "バンク先行有利度別": bucket_stats(bank_lead_bucket),
        "レースステージ別": bucket_stats(race_stage_bucket),
        "季節別": bucket_stats(season_bucket),
        "グレード別": bucket_stats(grade_bucket),
        "買い目内平均競走得点別": bucket_stats(race_score_bucket),
        "買い目内脚質構成別": bucket_stats(leg_style_bucket),
        "人気集中度パターン別": bucket_stats(popularity_pattern_bucket),
    }

    # 単一条件(例:「グレード別」だけ)の集計は、他の要因との交絡(本当の原因が別にある)
    # を見分けられない。「G1は勝ちやすい」のような早計な判断を避けるため、
    # 意味がありそうな2軸の組み合わせも別途集計する(のんの指摘により追加)。
    # 組み合わせは母数が単一条件より減るため、最低サンプル数を高めに設定する。
    def combo_bucket(key_fn_a, label_a, key_fn_b, label_b):
        return bucket_stats(lambda p: f"{label_a}:{key_fn_a(p)} × {label_b}:{key_fn_b(p)}")

    combo_buckets = {
        "グレード×季節": combo_bucket(grade_bucket, "グレード", season_bucket, "季節"),
        "券種×勝率帯": combo_bucket(lambda p: p.bet_type, "券種", prob_bucket, "勝率帯"),
        "季節×バンク先行有利度": combo_bucket(season_bucket, "季節", bank_lead_bucket, "先行有利度"),
        "券種×ライン絡み": combo_bucket(lambda p: p.bet_type, "券種", line_bucket, "ライン"),
        "券種×人気集中度パターン": combo_bucket(lambda p: p.bet_type, "券種", popularity_pattern_bucket, "人気集中度"),
    }
    min_sample_for_combo = 8

    # 全ての切り口を横断し、サンプル数が一定以上(ノイズ除け)で実績が高い条件をランキング化する。
    # これが「集計結果を見て予想を修正する」ための最初の手がかりになる。
    min_sample_for_ranking = 5
    ranking = []
    for category, buckets in all_buckets.items():
        for key, v in buckets.items():
            if v["count"] >= min_sample_for_ranking:
                ranking.append({
                    "category": category,
                    "condition": key,
                    "count": v["count"],
                    "win_rate_pct": v["win_rate_pct"],
                    "expected_win_rate_pct": v["expected_win_rate_pct"],
                    "expectancy_pct": v["expectancy_pct"],
                    "expected_roi_pct": v["expected_roi_pct"],
                })
    ranking.sort(key=lambda x: -x["expectancy_pct"])

    # 全体の想定期待値・想定的中率(購入時点でAIが見積もっていた値の平均)
    # 注意: ev_pct_at_purchaseは「0%が損益分岐点」の表現(calc_ev_pctの定義)で保存されている。
    # 実績収支率(overall_roi_pct)は「100%が損益分岐点」の表現なので、そのまま並べて比較すると
    # 単位が100ポイントずれる。+100して揃える(外部監査により発覚したバグを修正)。
    #
    # さらに、レースごとに投資額が異なる(証拠金は日々変動するため)ため、単純平均では
    # 少額のレースと高額のレースが同じ重みになってしまい、実際に得ていた/失っていた金額
    # (想定収益)とズレる。実績収支率(payout/stake)が金額加重であるのに合わせ、
    # 想定側も金額加重で計算する(のんの指摘により修正)。
    win_prob_values = [p.win_prob_at_purchase for p in purchases if p.win_prob_at_purchase is not None]
    ev_purchases = [p for p in purchases if p.ev_pct_at_purchase is not None]
    expected_win_rate_pct = round(sum(win_prob_values) / len(win_prob_values) * 100, 1) if win_prob_values else None
    expected_stake_sum = sum(p.stake_amount for p in ev_purchases)
    expected_profit_sum = sum(p.stake_amount * p.ev_pct_at_purchase / 100 for p in ev_purchases)
    expected_roi_pct = (
        round((expected_profit_sum / expected_stake_sum + 1) * 100, 2) if expected_stake_sum else None
    )
    expected_profit_total = round(expected_profit_sum, 0) if ev_purchases else None
    overall_win_count = sum(1 for p in purchases if p.result == "win")
    overall_win_rate_pct = round(overall_win_count / len(purchases) * 100, 1)

    # 資金管理シミュレーション用: 投資額加重平均オッズ(実際に賭けてきたオッズの実態)。
    # 単純平均だと少額の買い目と高額の買い目が同じ重みになってしまうため、
    # 実績収支率と同じ考え方で投資額加重にする(のんの要望により追加)。
    odds_purchases = [p for p in purchases if p.odds_at_purchase is not None]
    avg_odds_weighted = (
        round(sum(p.stake_amount * p.odds_at_purchase for p in odds_purchases)
              / sum(p.stake_amount for p in odds_purchases), 2)
        if odds_purchases else None
    )

    # 「予想と実績のズレは、単なる偶然のブレか、それとも本当に予想が偏っているのか」を
    # 統計的に判定する(のんの指摘により追加)。二項検定: 予想確率が正しいとしたら、
    # 実績の的中数がこれ以下になる確率(片側p値)。小さいほど「偶然では説明しにくい」。
    # 注意(外部監査による指摘): 同一レース内の複数の買い目は独立試行ではない
    # (1レースにつき実際に成立する結果は基本1通りのため、同じレース内の買い目の
    # 的中・不的中は強く相関する)。「買い目単位」の検定はこの相関を無視しているため、
    # 実際より「有意」に見えすぎる可能性がある。参考値として、より妥当な
    # 「レース単位」の検定(そのレースの購入全体が黒字で終えたか)も併記する。
    calibration_significance = None
    if win_prob_values:
        purchases_with_prob = [p for p in purchases if p.win_prob_at_purchase is not None]
        wins_with_prob = sum(1 for p in purchases_with_prob if p.result == "win")
        n_with_prob = len(purchases_with_prob)
        avg_predicted_prob = sum(win_prob_values) / len(win_prob_values)
        p_value = calc.binomial_lower_tail_p(wins_with_prob, n_with_prob, avg_predicted_prob)

        # レース単位の検定(独立性の問題を避けるための補助指標)。
        # そのレースの購入全体で黒字(payout>=stake)だったレースの割合を、
        # 「想定期待値(roi換算)が100%以上なら黒字を期待する」という単純な基準と比較する。
        race_ids_with_purchase = sorted({p.race_id for p in purchases})
        race_level_results = []
        for rid in race_ids_with_purchase:
            race_purchases = [p for p in purchases if p.race_id == rid]
            race_stake = sum(p.stake_amount for p in race_purchases)
            race_payout = sum(p.payout_amount for p in race_purchases)
            race_level_results.append(1 if race_payout >= race_stake else 0)
        n_races = len(race_level_results)
        profit_races = sum(race_level_results)

        if p_value < 0.05:
            judgement = "予想が実態より高すぎる可能性が高い(偶然では説明しにくい)"
        elif p_value < 0.20:
            judgement = "やや予想が高めだが、まだ偶然の範囲とも言える"
        else:
            judgement = "現時点のサンプル数では、偶然のブレの範囲内"
        calibration_significance = {
            "p_value_pct": round(p_value * 100, 4),
            "judgement": judgement,
            "n_used": n_with_prob,
            "wins_used": wins_with_prob,
            "predicted_prob_used_pct": round(avg_predicted_prob * 100, 4),
            "note": "総ベット数と一致しない場合、win_prob_at_purchase未記録の購入(手動記録分等)が混ざっています",
            "race_level": {
                "n_races": n_races,
                "profit_races": profit_races,
                "profit_race_rate_pct": round(profit_races / n_races * 100, 1) if n_races else None,
                "note": "「1レース=1試行」として、そのレースの購入全体が黒字だったか(同一レース内の買い目間の相関を避けた、より妥当な参考値。ただしレース数が少ないと検出力は低い)",
            },
        }

    return {
        "overall_expectancy_pct": round(overall_expectancy_pct, 2),
        "overall_roi_pct": round(overall_expectancy_pct + 100, 2),
        "overall_profit_total": round(total_payout - total_stake, 0),
        "expected_roi_pct": expected_roi_pct,
        "expected_profit_total": expected_profit_total,
        "overall_win_rate_pct": overall_win_rate_pct,
        "expected_win_rate_pct": expected_win_rate_pct,
        "avg_odds_weighted": avg_odds_weighted,
        "calibration_significance": calibration_significance,
        "total_bets": len(purchases),
        "best_conditions_ranking": ranking[:10],
        "worst_conditions_ranking": ranking[-10:][::-1] if len(ranking) > 10 else [],
        "by_bet_type": all_buckets["券種別"],
        "by_win_prob_bucket": all_buckets["勝率帯別"],
        "by_bank": all_buckets["バンク別"],
        "by_line_match": all_buckets["ライン絡み別"],
        "by_bank_lead_advantage": all_buckets["バンク先行有利度別"],
        "by_race_stage": all_buckets["レースステージ別"],
        "by_season": all_buckets["季節別"],
        "by_grade": all_buckets["グレード別"],
        "by_race_score": all_buckets["買い目内平均競走得点別"],
        "by_leg_style": all_buckets["買い目内脚質構成別"],
        "by_popularity_pattern": all_buckets["人気集中度パターン別"],
        "combo_buckets": {
            k: {kk: vv for kk, vv in v.items() if vv["count"] >= min_sample_for_combo}
            for k, v in combo_buckets.items()
        },
        "odds_drift": _odds_drift_stats(purchases),
        "note": (
            "「実績」は0%が損益分岐点、「実績収支率」は100%が損益分岐点の表現です(同じ数字を2通りの基準で表しているだけです)。"
            f"件数{min_sample_for_ranking}件未満の条件はランキングから除外しています(判断が不安定なため)。"
            f"組み合わせ集計は件数{min_sample_for_combo}件未満のマスを非表示にしています(単一条件よりサンプルが減るため基準を厳しめにしています)。"
        ),
    }


def _odds_drift_stats(purchases):
    """
    ①的中率の精度とは別に、②投票時オッズ→最終オッズのズレだけを検証する。
    券種によってズレ幅が大きく異なる(のんの実測: 3連単は約1割、ワイドは約4割)ため、
    全体平均に加えて券種別の内訳も返す。
    (最終オッズが未記録の購入は対象外)
    """
    with_final = [p for p in purchases if p.final_odds is not None and p.odds_at_purchase]
    if not with_final:
        return {"message": "最終オッズが記録された購入がまだありません"}

    drifts = [
        (p.final_odds - p.odds_at_purchase) / p.odds_at_purchase * 100
        for p in with_final
    ]
    avg_drift_pct = sum(drifts) / len(drifts)
    worsened_count = sum(1 for d in drifts if d < 0)  # オッズが下がる=自分に不利な方向

    by_bet_type = {}
    for p in with_final:
        d = (p.final_odds - p.odds_at_purchase) / p.odds_at_purchase * 100
        by_bet_type.setdefault(p.bet_type, []).append(d)
    by_bet_type_stats = {
        bt: {
            "sample_count": len(ds),
            "avg_odds_drift_pct": round(sum(ds) / len(ds), 2),
            "worst_odds_drift_pct": round(min(ds), 2),
        }
        for bt, ds in by_bet_type.items()
    }

    return {
        "sample_count": len(with_final),
        "avg_odds_drift_pct": round(avg_drift_pct, 2),
        "worsened_ratio_pct": round(worsened_count / len(with_final) * 100, 1),
        "by_bet_type": by_bet_type_stats,
        "note": "マイナスは投票時より最終オッズが下がった(不利な方向に動いた)ことを意味します",
    }


@router.post("/skipped")
def record_skipped(
    race_id: int,
    bet_type: str,
    combination: str,
    win_prob_estimated: float,
    ev_pct_estimated: float,
    reason: str,
    db: Session = Depends(get_db),
):
    """見送った買い目を記録する。結果が判明したら別途PATCHで actual_result を埋める運用。"""
    obj = models.SkippedBet(
        race_id=race_id,
        bet_type=bet_type,
        combination=combination,
        win_prob_estimated=win_prob_estimated,
        ev_pct_estimated=ev_pct_estimated,
        reason=reason,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/skipped/{skipped_id}/result")
def update_skipped_result(skipped_id: int, actual_result: str, actual_payout: float = 0, db: Session = Depends(get_db)):
    obj = db.query(models.SkippedBet).get(skipped_id)
    if not obj:
        raise HTTPException(404, "見送り記録が見つかりません")
    obj.actual_result = actual_result
    obj.actual_payout = actual_payout
    db.commit()
    return obj


@router.get("/skipped/stats")
def skipped_stats(db: Session = Depends(get_db)):
    """見送りが正しかったか(機会損失/機会回避)を集計する。"""
    skipped = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None)).all()
    if not skipped:
        return {"message": "結果判明済みの見送り記録がまだありません"}

    correct_skips = sum(1 for s in skipped if s.actual_result == "lose")
    missed_opportunities = [s for s in skipped if s.actual_result == "win"]
    missed_profit = sum((s.actual_payout or 0) for s in missed_opportunities)

    return {
        "total_skipped_evaluated": len(skipped),
        "correct_skip_pct": round(correct_skips / len(skipped) * 100, 1),
        "missed_opportunities_count": len(missed_opportunities),
        "missed_profit_total": missed_profit,
    }
