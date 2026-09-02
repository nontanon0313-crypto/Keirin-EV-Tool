from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from typing import Optional
from datetime import datetime

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
    # 証拠金残高は購入・結果確定では動かさない(証拠金はユーザー自身の資金管理として
    # 独立させ、予想・投票プラン・集計・検証には影響させない。のんの要望により変更)
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

    # 証拠金残高は動かさない(上記と同じ理由)
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
    # 証拠金残高は動かさない(証拠金はユーザー自身の資金管理として独立させる方針)
    return obj


def get_calibration_factors(db: Session) -> dict:
    """
    勝率帯ごとの自動補正係数を計算する。
    試行数が「200÷帯の代表確率」に達した帯だけ、実績に基づく補正係数を返す(段階的補正)。

    重要: Purchase(実際に買った分)だけでなく、SkippedBet(大穴帯除外等で見送った分、
    結果確定済みのもの)も合わせて集計する。以前はPurchaseしか見ておらず、
    大穴帯は「投票から除外され続ける限りデータも永遠に貯まらない」状態になっていた。
    見送った買い目も「予想確率 vs 実際の結果」というデータとしては全く同じ形なので、
    的中検証・自動補正には活用できる(のんの指摘により修正。投票対象からの除外と、
    集計・検証対象からの除外は別問題)。

    【既知の限界(のんの指摘により判明)】
    このrecordsは過去にSkippedBetとして「実際に記録された」ものに限られる。
    以前は期待値マイナスの組み合わせを記録しない仕様だったため、この係数は
    偏ったサンプルで学習されている可能性がある。偏りの無い検証は
    get_calibration_factors_retroactive()を参照。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    skipped = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None)).all()

    # Purchase/SkippedBetを「予想確率・的中したか・情報源」という共通の形に正規化して結合する
    # 補正係数は「補正前の予想 vs 実績」から学ぶ。rawが無い旧データは
    # win_prob_at_purchase(当時の判断値)にフォールバックする。
    # (prob, won, src, bet_type)
    records = []
    for p in purchases:
        prob = p.win_prob_raw if getattr(p, "win_prob_raw", None) is not None else p.win_prob_at_purchase
        if prob is not None:
            records.append((prob, p.result == "win", "purchase", p.bet_type))
    for s in skipped:
        prob = s.win_prob_raw if getattr(s, "win_prob_raw", None) is not None else s.win_prob_estimated
        if prob is not None:
            records.append((prob, s.actual_result == "win", "skipped", s.bet_type))

    return _compute_calibration_factors_from_records(records)


import time as _time

_retroactive_calibration_cache = {"computed_at": 0.0, "value": None}
RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS = 15 * 60  # 15分


def get_calibration_factors_retroactive(db: Session, use_cache: bool = True) -> dict:
    """
    Purchase/SkippedBetの記録(過去の運用ロジックの挙動に依存し、偏りがあり得る)に
    頼らず、確定済みレース全件・オッズが存在する組み合わせを毎回全て使って
    現在の確率モデルで再計算し、同じ形式で補正係数を返す。

    のんの指摘により追加: 以前は期待値マイナスの組み合わせを検証記録に残して
    いなかったため、補正係数がその偏ったサンプルで学習・評価されており、
    「補正が効いているように見えていたのは自己参照的な見かけだけだった」
    ことが判明した。この関数はその偏りを受けない、独立した検証経路。

    2026-09-01: 比較の結果、この方式の方が明らかに実績に近いことが確認できたため、
    app/routers/ev.pyの本番投票ロジックからも使用するようになった。
    ただし確定済みレース全件・オッズ全件を毎回スキャンする重い処理のため、
    日次パイプラインで1レースごとに呼ばれても再計算しすぎないよう、
    プロセス内メモリに15分キャッシュする(use_cache=Falseで強制再計算可能。
    比較エンドポイントは常に最新を見せたいのでキャッシュを使わない)。
    """
    now = _time.time()
    if use_cache:
        cached = _retroactive_calibration_cache["value"]
        if cached is not None and (now - _retroactive_calibration_cache["computed_at"]) < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS:
            return cached

    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    records = []
    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            continue
        odds_rows = db.query(models.Odds).filter(models.Odds.race_id == race.id).all()
        if not odds_rows:
            continue
        parsed_result = calc.parse_actual_result(race.actual_result)
        line_map, line_boost = calc.line_map_from_race(race)
        for o in odds_rows:
            if o.bet_type not in TARGET_BET_TYPES:
                continue
            try:
                cars = tuple(int(x) for x in o.combination.split("-"))
            except (ValueError, AttributeError):
                continue
            prob_raw = calc.estimate_prob_for_bet(win_probs, o.bet_type, cars, line_map=line_map, line_boost=line_boost)
            won = calc.judge_purchase_result(o.bet_type, o.combination, parsed_result)
            records.append((prob_raw, won, "retroactive", o.bet_type))

    result = _compute_calibration_factors_from_records(records)
    _retroactive_calibration_cache["value"] = result
    _retroactive_calibration_cache["computed_at"] = now
    return result


def _compute_calibration_factors_from_records(records: list) -> dict:
    """
    get_calibration_factors / get_calibration_factors_retroactiveの共通ロジック。
    records: [(prob, won, src, bet_type), ...]
    """

    # --- 全体補正係数(想定的中率を実績的中率に寄せる本体) ---
    overall_info = None
    if records:
        n_all = len(records)
        wins_all = sum(1 for _, won, _, _ in records if won)
        actual_all = wins_all / n_all
        predicted_all = sum(prob for prob, _, _, _ in records) / n_all
        if predicted_all > 0:
            raw_overall = calc.compute_calibration_factor(actual_all, predicted_all)
            p_overall = calc.binomial_lower_tail_p(wins_all, n_all, predicted_all)
            # 全体はサンプルが多いので、必要数を控えめにし係数をしっかり効かせる
            required_overall = 200
            overall_factor = calc.shrunk_calibration_factor(
                raw_overall, n_all, required_overall, p_value=p_overall
            )
            overall_info = {
                "sample_count": n_all,
                "required_sample_count": required_overall,
                "is_reliable": n_all >= required_overall,
                "actual_win_rate_pct": round(actual_all * 100, 4),
                "predicted_avg_prob_pct": round(predicted_all * 100, 4),
                "deviation_pct": round((actual_all - predicted_all) * 100, 4),
                "significance_p_value_pct": round(p_overall * 100, 4),
                "calibration_factor": round(overall_factor, 4),
                "prediction_accuracy_pct": calc.prediction_accuracy_pct(actual_all, predicted_all),
            }

    result = {}
    if overall_info is not None:
        result["overall"] = overall_info

    for lo, hi, name, mid in calc.PROB_BUCKETS:
        bucket_records = [(prob, won, src) for prob, won, src, _bt in records if lo <= prob < hi]
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

    # 券種ごとの補正係数(足切りではなく確率に掛ける用)。全券種共通の学習。
    by_bet_type = {}
    for bt in sorted({r[3] for r in records if r[3]}):
        bt_recs = [(prob, won) for prob, won, _src, b in records if b == bt]
        n = len(bt_recs)
        if n < 30:
            continue
        wins = sum(1 for _, w in bt_recs if w)
        actual = wins / n
        predicted = sum(pr for pr, _ in bt_recs) / n
        if predicted <= 0:
            continue
        raw_factor = calc.compute_calibration_factor(actual, predicted)
        required = max(50, int(200 / max(predicted, 0.01)))
        p_val = calc.binomial_lower_tail_p(wins, n, predicted)
        factor = calc.shrunk_calibration_factor(raw_factor, n, required, p_value=p_val)
        by_bet_type[bt] = {
            "sample_count": n,
            "actual_win_rate_pct": round(actual * 100, 2),
            "predicted_avg_prob_pct": round(predicted * 100, 2),
            "calibration_factor": round(factor, 3),
        }
    result["by_bet_type"] = by_bet_type

    # 券種×勝率帯の交差係数(のんの分析結果を受けて追加)。
    # 「想定勝率帯が上がるほど、ワイド・2車・3連複を中心に想定と実績の乖離が
    # 大きい」という発見に対応するため、勝率帯単体・券種単体それぞれの平均では
    # 薄まってしまう「この券種×この帯」特有のズレを直接学習する。
    # 必要サンプル数・段階的補正(shrinkage)の考え方は勝率帯単体の補正と全く同じ
    # 関数をそのまま使う(新しい閾値は作らない)。
    by_bet_type_bucket = {}
    for bt in sorted({r[3] for r in records if r[3]}):
        bucket_map = {}
        for lo, hi, name, mid in calc.PROB_BUCKETS:
            cell = [(prob, won) for prob, won, _src, b in records if b == bt and lo <= prob < hi]
            n = len(cell)
            if n < 30:
                continue
            wins = sum(1 for _, w in cell if w)
            actual = wins / n
            predicted = sum(pr for pr, _ in cell) / n
            if predicted <= 0:
                continue
            raw_factor = calc.compute_calibration_factor(actual, predicted)
            required = calc.required_sample_size(mid)
            p_val = calc.binomial_lower_tail_p(wins, n, predicted)
            factor = calc.shrunk_calibration_factor(raw_factor, n, required, p_value=p_val)
            bucket_map[name] = {
                "sample_count": n,
                "required_sample_count": required,
                "actual_win_rate_pct": round(actual * 100, 2),
                "predicted_avg_prob_pct": round(predicted * 100, 2),
                "deviation_pct": round((actual - predicted) * 100, 2),
                "significance_p_value_pct": round(p_val * 100, 4),
                "calibration_factor": round(factor, 3),
            }
        if bucket_map:
            by_bet_type_bucket[bt] = bucket_map
    result["by_bet_type_bucket"] = by_bet_type_bucket

    return result


DEFAULT_ODDS_SAFETY_MARGIN_PCT = 20.0

def get_stage_expectancy_map(db: Session, min_samples: int = 50) -> dict:
    """
    レースステージごとの実績収支率(回収率-100)を返す。
    サンプルが min_samples 未満のステージは含めない。
    戻り値: {stage_name: {"n": int, "expectancy_pct": float, "win_rate_pct": float}}
    """
    rows = (
        db.query(models.Purchase, models.Race.race_stage)
        .join(models.Race, models.Race.id == models.Purchase.race_id)
        .filter(models.Purchase.result != "pending")
        .filter(models.Race.race_stage.isnot(None))
        .all()
    )
    buckets = {}
    for p, stage in rows:
        if not stage:
            continue
        b = buckets.setdefault(stage, {"stake": 0.0, "payout": 0.0, "n": 0, "wins": 0})
        b["stake"] += p.stake_amount or 0
        b["payout"] += p.payout_amount or 0
        b["n"] += 1
        if p.result == "win":
            b["wins"] += 1
    out = {}
    for stage, b in buckets.items():
        if b["n"] < min_samples or b["stake"] <= 0:
            continue
        exp = (b["payout"] - b["stake"]) / b["stake"] * 100
        out[stage] = {
            "n": b["n"],
            "expectancy_pct": round(exp, 2),
            "win_rate_pct": round(b["wins"] / b["n"] * 100, 2),
        }
    return out


def get_bet_type_expectancy_map(db: Session, min_samples: int = 50) -> dict:
    """券種ごとの実績収支率。"""
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    buckets = {}
    for p in purchases:
        b = buckets.setdefault(p.bet_type, {"stake": 0.0, "payout": 0.0, "n": 0, "wins": 0})
        b["stake"] += p.stake_amount or 0
        b["payout"] += p.payout_amount or 0
        b["n"] += 1
        if p.result == "win":
            b["wins"] += 1
    out = {}
    for bt, b in buckets.items():
        if b["n"] < min_samples or b["stake"] <= 0:
            continue
        exp = (b["payout"] - b["stake"]) / b["stake"] * 100
        out[bt] = {
            "n": b["n"],
            "expectancy_pct": round(exp, 2),
            "win_rate_pct": round(b["wins"] / b["n"] * 100, 2),
        }
    return out



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
def investment_readiness(since: Optional[str] = None, db: Session = Depends(get_db)):
    """
    「実資金を投資してよいか」を、具体的な数値基準で自動判定する
    (のんの要望により追加)。
    基準:
    1. サンプル数が十分か(統計的な結論を出すのに足る量か)
    2. 予想と実績のズレが統計的に有意でないか(偶然の範囲に収まっているか)
    3. 実績収支率が黒字か、レース単位でも安定してプラスが多いか
    4. 実績の勝率・オッズで運用した場合、破産確率が十分低いか

    sinceクエリパラメータ(ISO日時、または'calibration_switch'ショートカット)を
    指定すると、その日時以降に作成された購入だけで判定する
    (のんの要望により追加: 補正係数の切り替え前の古い判断が混ざると、
    切り替えの効果が全期間の数字に埋もれて見えなくなるため)。
    """
    since_dt = _parse_since_param(since)
    purchases_query = db.query(models.Purchase).filter(models.Purchase.result != "pending")
    if since_dt:
        purchases_query = purchases_query.filter(models.Purchase.purchased_at >= since_dt)
    purchases = purchases_query.all()
    if not purchases:
        return {"ready": False, "message": "まだ確定した購入履歴がありません。", "since": since, "since_resolved": since_dt.isoformat() if since_dt else None}

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
            initial_bankroll=calc.FIXED_STAKING_BANKROLL, win_prob=win_rate, odds_value=avg_odds,
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
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
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


def _purchase_gap_block(purchases, prob_getter):
    """購入リストから実績的中 vs 予想平均の乖離ブロックを作る。"""
    rows = []
    for p in purchases:
        prob = prob_getter(p)
        if prob is None:
            continue
        rows.append((float(prob), p.result == "win"))
    if not rows:
        return None
    n = len(rows)
    wins = sum(1 for _, w in rows if w)
    actual = wins / n
    predicted = sum(pr for pr, _ in rows) / n
    p_value = calc.binomial_lower_tail_p(wins, n, predicted) if predicted > 0 else 1.0
    return {
        "sample_count": n,
        "wins": wins,
        "actual_win_rate_pct": round(actual * 100, 2),
        "predicted_avg_prob_pct": round(predicted * 100, 2),
        "deviation_pct": round((actual - predicted) * 100, 2),
        "significance_p_value_pct": round(p_value * 100, 4),
    }


@router.get("/calibration")
def calibration_status(db: Session = Depends(get_db)):
    """
    キャリブレーションの「効き」が分かる指標を返す。

    以前は全期間購入の1本の乖離(例: -5.8pt)を先頭に出していたが、
    母数が大きく数日ではほぼ動かないため「効いていない」ように見えていた。
    主指標を次に切り替える:
      1) 補正の効き(同じ購入に raw と 補正後を当てた before/after)
      2) 直近3日・7日・14日の購入(購入時点の勝率 vs 実績)
    全期間の乖離は参考値として残す。
    """
    from datetime import datetime, timedelta

    buckets = get_calibration_factors(db)

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending", models.Purchase.win_prob_at_purchase.isnot(None))
        .all()
    )

    # 参考: 全期間(購入時点の勝率) — 主指標にはしない
    overall = _purchase_gap_block(purchases, lambda p: p.win_prob_at_purchase)

    # 1) 補正の効き: raw がある購入だけ before/after
    with_raw = [p for p in purchases if getattr(p, "win_prob_raw", None) is not None]
    before = _purchase_gap_block(with_raw, lambda p: p.win_prob_raw)
    # 補正後は「今の係数を raw に掛けた値」ではなく、保存済み win_prob_at_purchase
    # (購入時に補正が掛かっていればそれが入る)。比較用に raw×factor も計算する。
    factor_overall = None
    by_bt = None
    by_bt_bucket = None
    bucket_only = buckets
    if isinstance(buckets, dict):
        factor_overall = buckets.get("overall")
        by_bt = buckets.get("by_bet_type")
        by_bt_bucket = buckets.get("by_bet_type_bucket")
        bucket_only = {
            k: v for k, v in buckets.items()
            if k not in ("by_bet_type", "by_bet_type_bucket", "overall")
        }

    def _apply_factor_to_raw(p):
        raw = p.win_prob_raw
        if raw is None:
            return None
        # 簡易: 全体係数のみ(詳細な交差は compare API 側)。効きの方向を見る用途。
        f = 1.0
        if factor_overall and factor_overall.get("calibration_factor"):
            f = float(factor_overall["calibration_factor"])
        return max(1e-9, min(0.99, raw * f))

    after_virtual = _purchase_gap_block(with_raw, _apply_factor_to_raw)
    effectiveness = None
    if before and after_virtual:
        # 乖離の絶対値が縮んだ量(pt)。プラスなら補正が効いている。
        improved = abs(before["deviation_pct"]) - abs(after_virtual["deviation_pct"])
        effectiveness = {
            "n": before["sample_count"],
            "before_deviation_pt": before["deviation_pct"],
            "after_deviation_pt": after_virtual["deviation_pct"],
            "improvement_pt": round(improved, 2),
            "before_accuracy_hint": before.get("predicted_avg_prob_pct"),
            "after_accuracy_hint": after_virtual.get("predicted_avg_prob_pct"),
            "判定": (
                "効いている(乖離が縮んだ)" if improved > 0.5
                else ("ほぼ横ばい" if improved > -0.5 else "効いていない(乖離が拡大)")
            ),
            "説明": (
                "同じ購入データに対し、補正前(raw)と全体係数適用後の乖離を比較。"
                "全期間の1本のズレが動かなくても、ここで縮んでいれば補正自体は機能している。"
            ),
        }

    # 2) 直近ウィンドウ: レース開催日(race_date)基準で切る。
    # purchased_at は一括取込日になりやすく、全件が「直近3日」に入ってしまうため使わない。
    from datetime import date as date_cls
    today = datetime.utcnow().date()
    race_rows = (
        db.query(models.Purchase, models.Race.race_date)
        .outerjoin(models.Race, models.Race.id == models.Purchase.race_id)
        .filter(
            models.Purchase.result != "pending",
            models.Purchase.win_prob_at_purchase.isnot(None),
        )
        .all()
    )
    dated = []
    no_date_count = 0
    for p, rd in race_rows:
        d = None
        if rd is not None:
            d = rd.date() if hasattr(rd, "date") and callable(rd.date) else rd
            if not isinstance(d, date_cls):
                try:
                    d = datetime.fromisoformat(str(d)[:10]).date()
                except Exception:
                    d = None
        if d is None:
            no_date_count += 1
            continue
        dated.append((p, d))

    recent = {}
    for days, key in ((3, "直近3日"), (7, "直近7日"), (14, "直近14日")):
        cutoff = today - timedelta(days=days)
        subset = [p for p, d in dated if d >= cutoff]
        block = _purchase_gap_block(subset, lambda x: x.win_prob_at_purchase)
        if block:
            block["window_days"] = days
            block["basis"] = "race_date"
            block["no_date_excluded"] = no_date_count
            recent[key] = block
        else:
            msg = f"{key}(開催日)に確定済み購入がありません"
            if no_date_count:
                msg += f"（開催日なし{no_date_count}件は除外）"
            recent[key] = {
                "sample_count": 0,
                "window_days": days,
                "basis": "race_date",
                "no_date_excluded": no_date_count,
                "メッセージ": msg,
            }

    return {
        # 主指標
        "effectiveness": effectiveness,
        "recent": recent,
        # 参考(主表示しない)
        "overall": overall,
        "factor_overall": factor_overall,
        "buckets": bucket_only,
        "by_bet_type": by_bt,
        "by_bet_type_bucket": by_bt_bucket,
        "message": (
            "【見方】主に「補正の効き」と「直近3日/7日」を見てください。"
            "全期間の1本の乖離は母数が大きく数日ではほぼ動きません(参考値)。"
            "新しいプランでは交差係数→勝率帯→全体係数の順で確率に掛けます(足切りではありません)。"
        ),
    }


@router.get("/calibration-compare")
def calibration_compare(db: Session = Depends(get_db)):
    """
    条件別に「補正前(raw)」と「補正後(calibrated)」の予想精度・乖離・p値を並べる。
    rawが無い旧レコードは before 側から除外し、件数を note で明示する。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    skipped = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None)).all()

    race_ids = {p.race_id for p in purchases} | {s.race_id for s in skipped}
    races_by_id = {
        r.id: r
        for r in db.query(models.Race).options(joinedload(models.Race.bank)).filter(models.Race.id.in_(race_ids)).all()
    } if race_ids else {}
    entries_by_race = {}
    if race_ids:
        for e in db.query(models.Entry).filter(models.Entry.race_id.in_(race_ids)).all():
            entries_by_race.setdefault(e.race_id, []).append(e)

    class Rec:
        __slots__ = ("race_id", "bet_type", "combination", "won", "prob_raw", "prob_cal", "source", "stake_amount", "payout_amount")
        def __init__(self, race_id, bet_type, combination, won, prob_raw, prob_cal, source, stake_amount=0.0, payout_amount=0.0):
            self.race_id = race_id
            self.bet_type = bet_type
            self.combination = combination
            self.won = won
            self.prob_raw = prob_raw
            self.prob_cal = prob_cal
            self.source = source
            self.stake_amount = stake_amount
            self.payout_amount = payout_amount

    recs = []
    n_without_raw = 0
    for p in purchases:
        raw = getattr(p, "win_prob_raw", None)
        cal = p.win_prob_at_purchase
        if cal is None and raw is None:
            continue
        if raw is None:
            n_without_raw += 1
        recs.append(Rec(p.race_id, p.bet_type, p.combination, p.result == "win", raw, cal, "purchase", p.stake_amount, p.payout_amount))
    for s in skipped:
        raw = getattr(s, "win_prob_raw", None)
        cal = s.win_prob_estimated
        if cal is None and raw is None:
            continue
        if raw is None:
            n_without_raw += 1
        recs.append(Rec(s.race_id, s.bet_type, s.combination, s.actual_result == "win", raw, cal, "skipped"))

    def metrics(prob_attr_recs):
        """list of (prob, won)"""
        if not prob_attr_recs:
            return None
        n = len(prob_attr_recs)
        wins = sum(1 for _, w in prob_attr_recs if w)
        actual = wins / n
        predicted = sum(p for p, _ in prob_attr_recs) / n
        accuracy = calc.prediction_accuracy_pct(actual, predicted)
        deviation_pt = round((actual - predicted) * 100, 2)
        p_value_pct = round(calc.binomial_lower_tail_p(wins, n, predicted) * 100, 2)
        return {
            "n": n,
            "wins": wins,
            "actual_win_rate_pct": round(actual * 100, 2),
            "predicted_avg_pct": round(predicted * 100, 2),
            "accuracy_pct": accuracy,
            "deviation_pt": deviation_pt,
            "p_value_pct": p_value_pct,
        }

    def line_bucket(r):
        race = races_by_id.get(r.race_id)
        if not race or not race.lines_data:
            return "ライン情報なし"
        line_map = {}
        for idx, line in enumerate(race.lines_data):
            for car in line:
                try:
                    line_map[int(car)] = idx
                except (TypeError, ValueError):
                    pass
        try:
            cars = [int(x) for x in r.combination.split("-")]
        except ValueError:
            return "ライン情報なし"
        line_ids = [line_map.get(c) for c in cars]
        if any(lid is None for lid in line_ids):
            return "ライン情報なし"
        return "同ライン絡み" if len(set(line_ids)) == 1 else "異なるライン混在"

    def lines_presence(r):
        race = races_by_id.get(r.race_id)
        if race and race.lines_data:
            return "並びあり"
        return "並びなし"

    def line_position(r):
        race = races_by_id.get(r.race_id)
        if not race or not race.lines_data:
            return "位置不明"
        pos_map = {}
        for line in race.lines_data:
            for i, car in enumerate(line):
                try:
                    pos_map[int(car)] = i  # 0=先頭
                except (TypeError, ValueError):
                    pass
        try:
            cars = [int(x) for x in r.combination.split("-")]
        except ValueError:
            return "位置不明"
        positions = [pos_map.get(c) for c in cars]
        if any(p is None for p in positions):
            return "位置不明"
        if all(p == 0 for p in positions):
            return "先頭のみ"
        if all(p is not None and p > 0 for p in positions):
            return "番手以降のみ"
        return "先頭と番手混在"

    def kimarite_bucket(r):
        entries = entries_by_race.get(r.race_id) or []
        by_car = {e.car_number: e for e in entries}
        try:
            cars = [int(x) for x in r.combination.split("-")]
        except ValueError:
            return "決まり手不明"
        labels = []
        for c in cars:
            e = by_car.get(c)
            if not e:
                labels.append("?")
                continue
            scores = {
                "逃": e.kimarite_nige or 0,
                "捲": e.kimarite_makuri or 0,
                "差": e.kimarite_sashi or 0,
                "マ": e.kimarite_mark or 0,
            }
            top = max(scores, key=scores.get)
            if scores[top] <= 0:
                labels.append("不明")
            else:
                labels.append(top)
        return "決まり手:" + "-".join(labels)

    def bank_bucket(r):
        race = races_by_id.get(r.race_id)
        return race.venue_name if race else "会場不明"

    def bet_type_bucket(r):
        return r.bet_type

    def prob_bucket_cal(r):
        prob = r.prob_cal if r.prob_cal is not None else r.prob_raw or 0
        name, _ = calc.get_prob_bucket(prob)
        return name

    def bet_type_x_prob_bucket(r):
        return f"{bet_type_bucket(r)} × {prob_bucket_cal(r)}"

    axes = {
        "券種別": bet_type_bucket,
        "勝率帯別": prob_bucket_cal,
        "券種×勝率帯(新設した交差補正)": bet_type_x_prob_bucket,
        "バンク別": bank_bucket,
        "ライン絡み別": line_bucket,
        "並び有無": lines_presence,
        "ライン内位置": line_position,
        "決まり手構成": kimarite_bucket,
    }

    def actual_roi(group):
        """
        実際に購入した分(見送りは除く)だけを使った実績収支率(100%が損益分岐点)。
        乖離(pt)とあわせて判断材料にする値(のんの要望により追加)。
        p値はサンプル数が多いほど、ごく小さなズレでも「有意」と出やすくなる性質があり、
        件数が万単位になった今はほぼ常に0%近辺に張り付いてしまうため、判断の主役には
        向かない。乖離(pt)と実績収支率の方が実態を素直に表す。
        """
        purchased = [r for r in group if r.stake_amount > 0]
        stake = sum(r.stake_amount for r in purchased)
        if stake <= 0:
            return None, 0
        payout = sum(r.payout_amount for r in purchased)
        return round((payout - stake) / stake * 100, 2), len(purchased)

    result_axes = {}
    for axis_name, key_fn in axes.items():
        buckets = {}
        for r in recs:
            key = key_fn(r)
            buckets.setdefault(key, []).append(r)
        rows = []
        for key, group in sorted(buckets.items(), key=lambda x: -len(x[1])):
            before_pairs = [(r.prob_raw, r.won) for r in group if r.prob_raw is not None]
            after_pairs = [(r.prob_cal, r.won) for r in group if r.prob_cal is not None]
            before = metrics(before_pairs)
            after = metrics(after_pairs)
            improved = None
            if before and after and before.get("accuracy_pct") is not None and after.get("accuracy_pct") is not None:
                improved = after["accuracy_pct"] >= before["accuracy_pct"]
            roi_pct, n_purchased = actual_roi(group)
            rows.append({
                "bucket": key,
                "n_total": len(group),
                "n_with_raw": len(before_pairs),
                "before": before,
                "after": after,
                "calibration_improved": improved,
                "actual_roi_pct": roi_pct,
                "n_purchased": n_purchased,
            })
        result_axes[axis_name] = rows

    # 総括(全条件合計)は、実際にお金を賭けた分だけで計算する(のんの指摘により修正。
    # 以前は見送りも含めた全件で計算しており、大穴帯の見送り件数が圧倒的に多いため、
    # 「自動補正の状態を確認」(実購入のみ・乖離-5.52pt)と、この総括(乖離-0.68pt)の
    # 数字が大きく食い違って見えていた)。
    purchased_recs = [r for r in recs if r.stake_amount > 0]
    overall_before = metrics([(r.prob_raw, r.won) for r in purchased_recs if r.prob_raw is not None])
    overall_after = metrics([(r.prob_cal, r.won) for r in purchased_recs if r.prob_cal is not None])
    overall_roi_pct, overall_n_purchased = actual_roi(recs)

    return {
        "n_records": len(recs),
        "n_without_raw": n_without_raw,
        "note": "補正前の数値は、補正前確率(win_prob_raw)が記録されているデータのみで計算しています。それが無い過去データは補正後の数値のみ計算しています。",
        "overall": {
            "before": overall_before,
            "after": overall_after,
            "actual_roi_pct": overall_roi_pct,
            "n_purchased": overall_n_purchased,
        },
        "axes": result_axes,
    }


TARGET_BET_TYPES = ["2車単", "2車複", "3連単", "3連複", "ワイド"]

# 2026-09-01: 補正係数をPurchase/SkippedBetベース(偏りあり)からretroactiveベース
# (偏り無し)へ切り替えた日時。それより前の購入は古い(過剰圧縮された)補正で
# 判断されたものなので、「切り替え後の成果だけを見たい」時の基準点として使う
# (のんの要望により追加: 全期間の集計だと過去の負債が混ざって、今回の修正が
# 効いているかどうか見えにくいため)。
CALIBRATION_SWITCH_AT = datetime(2026, 9, 1, 0, 0, 0)


def _parse_since_param(since: Optional[str]) -> Optional[datetime]:
    """
    'calibration_switch' というショートカットか、ISO日時文字列を受け取る。
    不正な値の場合はNone(絞り込み無し=全期間)を返す。
    """
    if not since:
        return None
    if since == "calibration_switch":
        return CALIBRATION_SWITCH_AT
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        return None


# app/routers/ev.pyで実際に生成されるSkippedBet.reasonの文言パターン。
# 「運用ゲート」= 券種・ステージ単位で機械的に見送りにしている仕組み(サンプル不足・
# 実績不振ステージ除外)。「購入判断」= 個々の買い目のEV・確率・金額を見て見送っている
# もの。この2つは原因が全く別なので、原文を推測で意味づけするのではなく、
# アプリ自身が生成する固定文言のプレフィックス一致でのみ分類する
# (ChatGPTの実装方針「reasonを推測で勝手に分類しない。既存コード上で明確に統一
# されている理由だけは正規化してよい」に従う)。
_SKIP_REASON_CATEGORY_RULES = [
    ("このステージの検証データ不足(", "運用ゲート:ステージサンプル不足(着順指定券種を一律見送り)"),
    ("不調ステージ除外(", "運用ゲート:実績不振ステージを丸ごと除外"),
    ("不調券種除外(", "運用ゲート:実績不振券種を丸ごと除外"),
    ("実績に基づくEV閾値未達(", "運用ゲート:実績ベースでEV閾値を引き上げ"),
    ("期待値マイナス(確率上位", "購入判断:期待値マイナス(確率上位N件のみ検証用に記録)"),
    ("買い示唆なし(EV/確率が閾値未満)", "購入判断:EV/確率が基準未満"),
    ("大穴帯(未補正)のため除外", "購入判断:大穴帯(未補正)のため除外"),
    ("ガミり回避のため除外", "購入判断:ガミり回避のため除外"),
    ("理論上の賭け金が最低単位", "購入判断:最低賭け金(100円)未満"),
]


def _categorize_skip_reason(reason: str) -> str:
    if not reason:
        return "理由未記録"
    for prefix, category in _SKIP_REASON_CATEGORY_RULES:
        if reason.startswith(prefix):
            return category
    return "その他(原文のまま)"


@router.get("/retroactive-capture-diagnostics")
def retroactive_capture_diagnostics(db: Session = Depends(get_db)):
    """
    過去の確定済みレースを、Purchase/SkippedBetの記録に一切頼らず、
    現在の確率モデル(ev.pyと同じロジックの複製、app/ev_calculator.py側)で
    その場で再計算して検証する。

    のんの要望により追加: 新規データ収集を待たなくても、既にDBにある
    Race・Entry・Oddsの過去データに対して「今のモデルなら、実際に勝った
    組み合わせを何位に予想できていたか」をすぐ確認できるようにするため。

    bet-type-diagnosticsとの違い:
    - bet-type-diagnosticsはPurchase/SkippedBetの記録(=過去の運用ロジックの
      挙動)を検証する。記録漏れ(過去のバグ等)があればその影響を受ける。
    - こちらはオッズが存在する組み合わせを毎回全て再評価するため、
      過去の記録方法に問題があっても影響を受けない、より純粋な検証。

    本番の投票ロジック(app/routers/ev.py)は一切呼び出さず、変更もしない。
    """
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    calibration_factors = get_calibration_factors(db)

    # (race_id, bet_type) -> [(combination, prob_raw, prob_cal, won), ...]
    groups_raw_by_type = {}
    groups_cal_by_type = {}
    flat_records_by_type = {}  # brier_score計算用

    races_evaluated = 0
    races_skipped_no_win_probs = 0
    races_skipped_no_odds = 0

    for race in races:
        entries = race.entries
        win_probs = calc.build_win_probs_from_entries(entries)
        if not win_probs:
            races_skipped_no_win_probs += 1
            continue

        odds_rows = db.query(models.Odds).filter(models.Odds.race_id == race.id).all()
        if not odds_rows:
            races_skipped_no_odds += 1
            continue

        parsed_result = calc.parse_actual_result(race.actual_result)
        line_map, line_boost = calc.line_map_from_race(race)

        races_evaluated += 1

        for o in odds_rows:
            if o.bet_type not in TARGET_BET_TYPES:
                continue
            try:
                cars = tuple(int(x) for x in o.combination.split("-"))
            except (ValueError, AttributeError):
                continue

            prob_raw = calc.estimate_prob_for_bet(win_probs, o.bet_type, cars, line_map=line_map, line_boost=line_boost)
            prob_cal = calc.apply_calibration_to_prob(prob_raw, calibration_factors, bet_type=o.bet_type)
            won = calc.judge_purchase_result(o.bet_type, o.combination, parsed_result)

            key = (race.id, o.bet_type)
            groups_raw_by_type.setdefault(o.bet_type, {}).setdefault(key, []).append((o.combination, prob_raw, won))
            groups_cal_by_type.setdefault(o.bet_type, {}).setdefault(key, []).append((o.combination, prob_cal, won))
            flat_records_by_type.setdefault(o.bet_type, []).append((prob_raw, prob_cal, won))

    result = {}
    for bt in TARGET_BET_TYPES:
        flat = flat_records_by_type.get(bt, [])
        if not flat:
            result[bt] = {"sample_count": 0}
            continue

        brier_raw = calc.brier_score([(r[0], r[2]) for r in flat])
        brier_cal = calc.brier_score([(r[1], r[2]) for r in flat])
        actual_win_rate = sum(1 for r in flat if r[2]) / len(flat)
        avg_prob_raw = sum(r[0] for r in flat) / len(flat)
        avg_prob_cal = sum(r[1] for r in flat) / len(flat)

        ranking_raw = calc.ranking_diagnostics(groups_raw_by_type.get(bt, {}))
        ranking_cal = calc.ranking_diagnostics(groups_cal_by_type.get(bt, {}))

        result[bt] = {
            "sample_count": len(flat),
            "n_races": len(groups_raw_by_type.get(bt, {})),
            "actual_win_rate_pct": round(actual_win_rate * 100, 2),
            "predicted_avg_prob_pct": {
                "raw": round(avg_prob_raw * 100, 2),
                "calibrated": round(avg_prob_cal * 100, 2),
            },
            "deviation_pct": {
                "raw": round((actual_win_rate - avg_prob_raw) * 100, 2),
                "calibrated": round((actual_win_rate - avg_prob_cal) * 100, 2),
            },
            "brier_score": {
                "raw": round(brier_raw, 4) if brier_raw is not None else None,
                "calibrated": round(brier_cal, 4) if brier_cal is not None else None,
            },
            "ranking_diagnostics": {
                "raw": ranking_raw,
                "calibrated": ranking_cal,
            },
        }

    return {
        "races_evaluated": races_evaluated,
        "races_skipped_no_win_probs": races_skipped_no_win_probs,
        "races_skipped_no_odds": races_skipped_no_odds,
        "by_bet_type": result,
        "message": (
            "これは記録(Purchase/SkippedBet)に一切頼らず、オッズが存在する"
            "組み合わせを毎回全て使って現在のモデルで再計算した結果です。"
            "ranking_diagnosticsのwinner_captured_rate_pctが低い場合、"
            "それはオッズが存在する組み合わせの中でAIの候補生成が漏らしている"
            "ことを意味します(このエンドポイントはオッズが存在する組み合わせを"
            "全て評価しているため、過去の記録漏れの影響を受けません)。"
        ),
    }


def _summarize_bucket(bucket: dict) -> dict:
    if not bucket:
        return {}
    return {
        "sample_count": bucket.get("sample_count"),
        "actual_win_rate_pct": bucket.get("actual_win_rate_pct"),
        "predicted_avg_prob_pct": bucket.get("predicted_avg_prob_pct"),
        "deviation_pct": bucket.get("deviation_pct"),
        "calibration_factor": bucket.get("calibration_factor"),
    }


@router.get("/calibration-factors-compare")
def calibration_factors_compare(db: Session = Depends(get_db)):
    """
    現行のキャリブレーション係数(Purchase/SkippedBetの記録ベース。偏りの
    可能性あり)と、遡及検証ベース(オッズが存在する組み合わせを毎回全て使う、
    偏りの無い方法)の係数を並べて比較する。

    のんの指摘により追加: 「正しく修正し、キャリブレーションのやり直しを
    行えばいい」という方針に沿って、まず新旧の係数を比較できる形で出す
    (この時点では投票ロジックは一切変更しない)。比較結果を見て問題なければ、
    実際に使う係数を切り替える。
    """
    current = get_calibration_factors(db)
    retroactive = get_calibration_factors_retroactive(db, use_cache=False)

    bucket_names = [name for _lo, _hi, name, _mid in calc.PROB_BUCKETS]

    overall_compare = {
        "current": _summarize_bucket(current.get("overall") or {}),
        "retroactive": _summarize_bucket(retroactive.get("overall") or {}),
    }

    by_bucket_compare = {}
    for name in bucket_names:
        by_bucket_compare[name] = {
            "current": _summarize_bucket(current.get(name) or {}),
            "retroactive": _summarize_bucket(retroactive.get(name) or {}),
        }

    by_bet_type_compare = {}
    all_bts = set((current.get("by_bet_type") or {}).keys()) | set((retroactive.get("by_bet_type") or {}).keys())
    for bt in sorted(all_bts):
        by_bet_type_compare[bt] = {
            "current": _summarize_bucket((current.get("by_bet_type") or {}).get(bt) or {}),
            "retroactive": _summarize_bucket((retroactive.get("by_bet_type") or {}).get(bt) or {}),
        }

    by_bet_type_bucket_compare = {}
    cur_cross = current.get("by_bet_type_bucket") or {}
    retro_cross = retroactive.get("by_bet_type_bucket") or {}
    all_cross_bts = set(cur_cross.keys()) | set(retro_cross.keys())
    for bt in sorted(all_cross_bts):
        cell_compare = {}
        cur_cells = cur_cross.get(bt) or {}
        retro_cells = retro_cross.get(bt) or {}
        for name in bucket_names:
            if name in cur_cells or name in retro_cells:
                cell_compare[name] = {
                    "current": _summarize_bucket(cur_cells.get(name) or {}),
                    "retroactive": _summarize_bucket(retro_cells.get(name) or {}),
                }
        if cell_compare:
            by_bet_type_bucket_compare[bt] = cell_compare

    return {
        "overall": overall_compare,
        "by_bucket": by_bucket_compare,
        "by_bet_type": by_bet_type_compare,
        "by_bet_type_bucket": by_bet_type_bucket_compare,
        "message": (
            "currentは今まで実際の投票判断に使われてきた係数(Purchase/SkippedBet"
            "の記録ベース、偏りの可能性あり)。retroactiveはオッズが存在する組み合わせを"
            "毎回全て使う、偏りの無い方法で計算し直した係数。両者のdeviation_pctや"
            "calibration_factorが大きくずれている場合、現行の係数は偏ったサンプルで"
            "学習されていた可能性が高い。この比較を見て問題なければ、"
            "ev.pyが呼び出す関数をget_calibration_factors_retroactiveに切り替える。"
        ),
    }


@router.get("/bet-type-diagnostics")
def bet_type_diagnostics(since: Optional[str] = None, db: Session = Depends(get_db)):
    """
    券種別に、結果が悪い原因を次の段階へ分解して診断する。

    Stage 1: Candidate Capture
        的中買い目を候補集合に含められたか

    Stage 2: Ranking
        捕捉した的中買い目を候補上位に置けたか

    Stage 3: Purchase Survival
        捕捉した的中買い目が実際の購入まで残ったか

    Stage 4: Probability
        予測確率が実績とどれだけ一致しているか

    Stage 5: Monetization
        実際に購入した分がROIとして利益化できているか

    PurchaseとSkippedBetの両方を対象にする。
    DBスキーマ変更は不要。

    sinceクエリパラメータ(ISO日時、または'calibration_switch'ショートカット)を
    指定すると、その日時以降に作成された購入・見送りだけで診断する
    (investment-readinessと同じ理由で追加)。
    """
    since_dt = _parse_since_param(since)

    purchases_q = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.bet_type.in_(TARGET_BET_TYPES))
    )
    skipped_q = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.isnot(None))
        .filter(models.SkippedBet.bet_type.in_(TARGET_BET_TYPES))
    )
    if since_dt:
        purchases_q = purchases_q.filter(models.Purchase.purchased_at >= since_dt)
        skipped_q = skipped_q.filter(models.SkippedBet.created_at >= since_dt)

    purchases = purchases_q.all()
    skipped = skipped_q.all()

    class Rec:
        __slots__ = (
            "race_id",
            "bet_type",
            "combination",
            "prob_raw",
            "prob_cal",
            "won",
            "is_purchase",
            "stake_amount",
            "payout_amount",
            "skip_reason",
        )

        def __init__(
            self,
            race_id,
            bet_type,
            combination,
            prob_raw,
            prob_cal,
            won,
            is_purchase,
            stake_amount=0.0,
            payout_amount=0.0,
            skip_reason=None,
        ):
            self.race_id = race_id
            self.bet_type = bet_type
            self.combination = combination
            self.prob_raw = prob_raw
            self.prob_cal = prob_cal
            self.won = won
            self.is_purchase = is_purchase
            self.stake_amount = stake_amount or 0.0
            self.payout_amount = payout_amount or 0.0
            self.skip_reason = skip_reason

    all_records = []

    for p in purchases:
        prob_raw = (
            p.win_prob_raw
            if getattr(p, "win_prob_raw", None) is not None
            else p.win_prob_at_purchase
        )

        all_records.append(
            Rec(
                race_id=p.race_id,
                bet_type=p.bet_type,
                combination=p.combination,
                prob_raw=prob_raw,
                prob_cal=p.win_prob_at_purchase,
                won=(p.result == "win"),
                is_purchase=True,
                stake_amount=p.stake_amount,
                payout_amount=p.payout_amount,
            )
        )

    for s in skipped:
        prob_raw = (
            s.win_prob_raw
            if getattr(s, "win_prob_raw", None) is not None
            else s.win_prob_estimated
        )

        all_records.append(
            Rec(
                race_id=s.race_id,
                bet_type=s.bet_type,
                combination=s.combination,
                prob_raw=prob_raw,
                prob_cal=s.win_prob_estimated,
                won=(s.actual_result == "win"),
                is_purchase=False,
                skip_reason=getattr(s, "reason", None),
            )
        )

    def _pct(numerator, denominator):
        if denominator <= 0:
            return None
        return round(numerator / denominator * 100, 1)

    def _probability_metrics(records, prob_attr):
        pairs = [
            (getattr(r, prob_attr), r.won)
            for r in records
            if getattr(r, prob_attr) is not None
        ]

        if not pairs:
            return {
                "n": 0,
                "actual_win_rate_pct": None,
                "predicted_avg_prob_pct": None,
                "deviation_pct": None,
                "brier_score": None,
            }

        n = len(pairs)
        wins = sum(1 for _, won in pairs if won)
        actual = wins / n
        predicted = sum(prob for prob, _ in pairs) / n
        brier = calc.brier_score(pairs)

        return {
            "n": n,
            "actual_win_rate_pct": round(actual * 100, 2),
            "predicted_avg_prob_pct": round(predicted * 100, 2),
            "deviation_pct": round((actual - predicted) * 100, 2),
            "brier_score": round(brier, 4) if brier is not None else None,
        }

    def _build_ranking_groups(records, prob_attr):
        groups = {}

        for r in records:
            prob = getattr(r, prob_attr)
            if prob is None:
                continue

            groups.setdefault(r.race_id, []).append(
                (r.combination, prob, r.won)
            )

        return groups

    def _purchase_funnel(records, bet_type):
        """
        レース単位で評価する。

        captured:
            そのレースの候補集合に的中買い目が存在する。

        winner_purchased:
            的中買い目のうち少なくとも1つが実際にPurchaseになっている。

        同着等で複数的中候補がある場合も、
        1つでも購入されていればsurvivedとする。

        捕捉できなかったレースについては、さらに2つに分離する
        (のんの指摘: 「投票が無い組み合わせはオッズ自体が存在しない。それは
        投票無しとして扱えばいい。box範囲の問題と一緒くたにしないでほしい」)。

        odds_unavailable:
            そのレース・その券種で、的中する買い目のオッズが
            oddspark側にそもそも1件も存在しなかった(=誰も投票していない
            組み合わせだった)。box範囲を広げても拾いようがない、
            構造上どうしようもないケース。

        candidate_generation_miss:
            オッズは存在していたのに、AIの候補生成(box/展開)が
            その的中買い目を候補に含めていなかった。これが本当に
            改善すべき「候補生成の問題」。
        """
        by_race = {}

        for r in records:
            by_race.setdefault(r.race_id, []).append(r)

        n_groups = 0
        captured_groups = 0
        winner_purchased_groups = 0
        winner_lost_groups = 0
        odds_unavailable_groups = 0
        candidate_generation_miss_groups = 0

        reason_counts = {}
        category_counts = {}

        for race_id, group in by_race.items():
            if not group:
                continue

            n_groups += 1

            winners = [r for r in group if r.won]

            if not winners:
                # 候補(購入+見送り)の中に的中買い目が無かったレース。
                # ここでさらに、実際にオッズが存在していたのに候補から漏れたのか、
                # そもそもオッズ自体が存在しなかったのかを確認する。
                race = db.get(models.Race, race_id)
                odds_had_winner = False
                if race and race.actual_result:
                    parsed_result = calc.parse_actual_result(race.actual_result)
                    odds_rows_for_race = (
                        db.query(models.Odds)
                        .filter(models.Odds.race_id == race_id)
                        .filter(models.Odds.bet_type == bet_type)
                        .all()
                    )
                    odds_had_winner = any(
                        calc.judge_purchase_result(bet_type, o.combination, parsed_result)
                        for o in odds_rows_for_race
                    )
                if odds_had_winner:
                    candidate_generation_miss_groups += 1
                else:
                    odds_unavailable_groups += 1
                continue

            captured_groups += 1

            if any(r.is_purchase for r in winners):
                winner_purchased_groups += 1
                continue

            winner_lost_groups += 1

            # 同着等で複数winnerがある場合は理由を全件カウントすると
            # グループ数を超えるため、同一レース内のreasonは重複除去。
            reasons = {
                (r.skip_reason or "理由未記録")
                for r in winners
                if not r.is_purchase
            }

            if not reasons:
                reasons = {"理由未記録"}

            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

            # カテゴリ側も同様に、同一レース内では重複除去してから集計する
            categories = {_categorize_skip_reason(r) for r in reasons}
            for category in categories:
                category_counts[category] = category_counts.get(category, 0) + 1

        not_captured_groups = odds_unavailable_groups + candidate_generation_miss_groups

        return {
            "n_groups": n_groups,
            "captured_winner_groups": captured_groups,
            "winner_purchased_groups": winner_purchased_groups,
            "winner_purchase_survival_rate_pct": _pct(
                winner_purchased_groups,
                captured_groups,
            ),
            "winner_lost_before_purchase_groups": winner_lost_groups,
            "winner_lost_before_purchase_rate_pct": _pct(
                winner_lost_groups,
                captured_groups,
            ),
            "not_captured_breakdown": {
                "odds_unavailable_groups": odds_unavailable_groups,
                "odds_unavailable_note": (
                    "誰も投票しておらずoddspark側にオッズ自体が無かった組み合わせ。"
                    "投票が無い=買いようがなかったケースなので、候補生成ロジックの"
                    "問題ではない(改善不要・構造上の上限)。"
                ),
                "candidate_generation_miss_groups": candidate_generation_miss_groups,
                "candidate_generation_miss_note": (
                    "オッズは存在していたのに、AIの候補生成(box範囲・展開)が"
                    "この的中買い目を候補に含めていなかった。ここが本当の改善対象。"
                ),
                "candidate_generation_miss_rate_pct": _pct(
                    candidate_generation_miss_groups,
                    not_captured_groups,
                ),
            },
            "winner_filter_loss_breakdown_by_category": dict(
                sorted(
                    category_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
            "winner_filter_loss_breakdown": dict(
                sorted(
                    reason_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
        }

    def _diagnose(
        n_groups,
        ranking,
        funnel,
        probability_metrics,
        purchase_count,
        roi_pct,
    ):
        """
        閾値だけで「原因確定」と断定しない。

        primary_issueは現データ上で最も先に確認すべき段階を示す。
        券種間の絶対比較だけではなく、同一券種の時系列比較にも使えるよう、
        evidenceを併記する。
        """
        evidence = []

        capture_rate = ranking.get("winner_captured_rate_pct")
        top_n = ranking.get("top_n_hit_rate_pct") or {}
        top1 = top_n.get("top1")
        top3 = top_n.get("top3")
        top5 = top_n.get("top5")

        survival_rate = funnel.get("winner_purchase_survival_rate_pct")

        deviation = probability_metrics.get("deviation_pct")
        brier = probability_metrics.get("brier_score")

        primary_issue = "データ不足または複合要因"
        secondary_issue = None
        recommended_action = (
            "サンプルを追加し、候補捕捉→ランキング→購入生存→確率→ROIの順で"
            "ボトルネックを比較してください。"
        )

        # 1. 候補捕捉
        if capture_rate is not None:
            evidence.append(
                f"winner_captured_rate={capture_rate}%"
            )

        not_captured = funnel.get("not_captured_breakdown", {}) or {}
        cgm_rate = not_captured.get("candidate_generation_miss_rate_pct")
        odds_unavailable_n = not_captured.get("odds_unavailable_groups")
        cgm_n = not_captured.get("candidate_generation_miss_groups")
        if cgm_rate is not None:
            evidence.append(
                f"捕捉できなかった中でオッズはあったのに候補から漏れた割合="
                f"{cgm_rate}%(候補生成漏れ{cgm_n}件/オッズ自体無し{odds_unavailable_n}件)"
            )

        # 2. ランキング
        if top1 is not None:
            evidence.append(f"Top1={top1}%")

        if top3 is not None:
            evidence.append(f"Top3={top3}%")

        # 3. 購入生存
        if survival_rate is not None:
            evidence.append(
                f"winner_purchase_survival={survival_rate}%"
            )

        # 4. 確率
        if deviation is not None:
            evidence.append(
                f"probability_deviation={deviation}pt"
            )

        if brier is not None:
            evidence.append(f"brier={brier}")

        # 購入数が極端に少ない場合、ROIを主因判定に使わない。
        roi_reliable = purchase_count >= 30

        # 優先順位:
        # 候補捕捉 → ランキング → 購入フィルタ → 確率 → ROI
        #
        # capture率は券種固有の候補数に左右されるため、
        # 「50%未満=必ず悪い」とはしない。
        # ただし、捕捉できた後のTop3との差が小さい場合は、
        # 捕捉段階がより有力なボトルネックになる。
        if (
            capture_rate is not None
            and top3 is not None
            and capture_rate < 50
            and top3 >= 70
            and (cgm_rate is None or cgm_rate >= 30)
        ):
            primary_issue = "候補生成・候補範囲"
            secondary_issue = "候補内ランキングは相対的に保たれている可能性"
            recommended_action = (
                "候補数、box範囲、展開候補を見直してください。"
                "確率補正だけでは候補外の的中買い目は救えません。"
                "ただしnot_captured_breakdownを見て、捕捉できなかった原因の"
                "大半が'オッズ自体無し'(誰も投票していない組み合わせ)なら、"
                "それは構造上の上限であり候補生成ロジックの問題ではありません。"
                "'候補生成漏れ'(オッズはあったのに候補から外れた)の比率が"
                "高い場合のみ、box/展開ロジックの改善に着手してください。"
            )

        elif (
            capture_rate is not None
            and capture_rate >= 20
            and top1 is not None
            and top3 is not None
            and top1 < 25
            and top3 - top1 >= 20
        ):
            primary_issue = "候補内ランキング"
            secondary_issue = "候補生成"
            recommended_action = (
                "候補自体には正解が入っているが上位へ押し上げられていない可能性があります。"
                "組み合わせ確率、Harville式への入力確率、ライン補正を優先検証してください。"
            )

        elif (
            survival_rate is not None
            and capture_rate is not None
            and capture_rate >= 20
            and survival_rate < 50
        ):
            primary_issue = "購入フィルタ"
            secondary_issue = "候補生成またはランキング"
            recommended_action = (
                "的中候補が候補集合に存在しているのに購入まで残っていない可能性があります。"
                "winner_filter_loss_breakdown_by_categoryを確認し、"
                "『運用ゲート』(サンプル不足・実績不振ステージによる機械的な見送り)由来か、"
                "『購入判断』(EV/確率閾値)由来かをまず区別してください。"
                "運用ゲート由来が大半なら、それは安全策が意図通り働いている結果であり、"
                "確率やEV計算そのものの問題ではありません。"
            )

        elif (
            deviation is not None
            and abs(deviation) >= 1.0
        ):
            primary_issue = "確率キャリブレーション"
            secondary_issue = "候補内ランキング"
            recommended_action = (
                "rawとcalibratedのBrier Scoreおよびランキングを比較してください。"
                "ランキングが変わらず乖離だけ縮むなら、予想能力ではなく確率尺度の問題です。"
            )

        elif roi_reliable and roi_pct is not None and roi_pct < 0:
            primary_issue = "収益化・オッズ/EV評価"
            secondary_issue = "確率または購入条件"
            recommended_action = (
                "候補捕捉・ランキング・購入生存が大きく崩れていない場合、"
                "投票時オッズと最終払戻、EV閾値、安全マージンを確認してください。"
            )

        confidence = "low"

        if n_groups >= 100:
            confidence = "high"
        elif n_groups >= 30:
            confidence = "medium"

        if purchase_count < 10:
            # 予想診断側のサンプル(n_groups)が多くても、
            # 購入ROIの判断は購入件数(purchase_count)が少なければ別途弱い。
            # confidenceの高低に関わらず、購入数が少ない場合は常に明示する。
            evidence.append(
                f"purchase_count={purchase_count}のためROI評価は不安定"
            )

        return {
            "primary_issue": primary_issue,
            "secondary_issue": secondary_issue,
            "recommended_action": recommended_action,
            "confidence": confidence,
            "evidence": evidence,
            "note": (
                "これは原因候補の優先順位であり、単一指標だけで原因を確定するものではありません。"
            ),
        }

    result = {}

    for bt in TARGET_BET_TYPES:
        bt_records = [
            r
            for r in all_records
            if r.bet_type == bt
        ]

        n = len(bt_records)

        if n == 0:
            result[bt] = {
                "sample_count": 0,
                "diagnosis": {
                    "primary_issue": "データ不足",
                    "confidence": "low",
                },
            }
            continue

        purchase_records = [
            r
            for r in bt_records
            if r.is_purchase
        ]

        n_purchased = len(purchase_records)

        stake_total = sum(
            r.stake_amount
            for r in purchase_records
        )

        payout_total = sum(
            r.payout_amount
            for r in purchase_records
        )

        roi_pct = (
            round(
                (payout_total / stake_total - 1) * 100,
                2,
            )
            if stake_total > 0
            else None
        )

        raw_metrics = _probability_metrics(
            bt_records,
            "prob_raw",
        )

        cal_metrics = _probability_metrics(
            bt_records,
            "prob_cal",
        )

        groups_raw = _build_ranking_groups(
            bt_records,
            "prob_raw",
        )

        groups_cal = _build_ranking_groups(
            bt_records,
            "prob_cal",
        )

        ranking_raw = calc.ranking_diagnostics(
            groups_raw,
        )

        ranking_cal = calc.ranking_diagnostics(
            groups_cal,
        )

        funnel = _purchase_funnel(
            bt_records,
            bt,
        )

        # 主診断は実際の購入判断に使われるcalibrated側を優先。
        # calibratedが無い場合はrawへフォールバック。
        ranking_for_diagnosis = (
            ranking_cal
            if ranking_cal.get("n_groups", 0) > 0
            else ranking_raw
        )

        probability_for_diagnosis = (
            cal_metrics
            if cal_metrics.get("n", 0) > 0
            else raw_metrics
        )

        diagnosis = _diagnose(
            n_groups=ranking_for_diagnosis.get("n_groups", 0),
            ranking=ranking_for_diagnosis,
            funnel=funnel,
            probability_metrics=probability_for_diagnosis,
            purchase_count=n_purchased,
            roi_pct=roi_pct,
        )

        result[bt] = {
            "sample_count": n,
            "purchase_count": n_purchased,
            "skipped_count": n - n_purchased,

            "actual_win_rate_pct": (
                cal_metrics["actual_win_rate_pct"]
                if cal_metrics["actual_win_rate_pct"] is not None
                else raw_metrics["actual_win_rate_pct"]
            ),

            # 既存レスポンス互換
            "predicted_avg_prob_pct": {
                "raw": raw_metrics["predicted_avg_prob_pct"],
                "calibrated": cal_metrics["predicted_avg_prob_pct"],
            },

            "deviation_pct": {
                "raw": raw_metrics["deviation_pct"],
                "calibrated": cal_metrics["deviation_pct"],
            },

            "brier_score": {
                "raw": raw_metrics["brier_score"],
                "calibrated": cal_metrics["brier_score"],
            },

            "ranking_diagnostics": {
                "raw": ranking_raw,
                "calibrated": ranking_cal,
            },

            # 新規
            "purchase_funnel": funnel,
            "winner_filter_loss_breakdown": (
                funnel["winner_filter_loss_breakdown"]
            ),
            "winner_filter_loss_breakdown_by_category": (
                funnel["winner_filter_loss_breakdown_by_category"]
            ),
            "diagnosis": diagnosis,

            "actual_roi_pct_purchased_only": roi_pct,
        }

    return {
        "by_bet_type": result,
        "diagnostic_stages": [
            "candidate_capture",
            "ranking",
            "purchase_survival",
            "probability",
            "monetization",
        ],
        "message": (
            "券種ごとに、候補生成→候補内ランキング→購入フィルタ→確率→ROIの順で"
            "原因を分離します。winner_captured_rateが低ければ候補生成側、"
            "capture後にTop1が低くTop3/Top5で改善するならランキング側、"
            "winner_purchase_survival_rateが低ければ購入フィルタ側を優先確認してください。"
            "rawとcalibratedでBrierや順位指標を比較し、確率尺度の改善と"
            "識別能力の改善を混同しないでください。"
        ),
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
    }



@router.get("/diagnostics/predicted-vs-actual-return")
def diagnostics_predicted_vs_actual_return(
    since: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    同一の確定済みPurchase集合について、
    1. 保存EVから逆算した予測払戻
    2. 購入時確率 × 購入時オッズから再計算した予測払戻
    3. 実際払戻
    を直接比較する。

    目的:
    「全体の的中率は校正されているのにEV/ROIが大きく乖離する」
    問題について、確率・EV・払戻・集計のどこで乖離しているかを
    同一母集団で確認する。
    """

    query = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
    )

    since_dt = None
    since_resolved = None

    if since:
        if since == "calibration_switch":
            # 既存の診断と同じ基準日を使うため、
            # Purchaseに作成日時が存在する場合のみ安全に絞る。
            # 基準日の解決ができない場合は全件対象とし、
            # レスポンスで明示する。
            pass
        else:
            try:
                since_dt = datetime.fromisoformat(
                    since.replace("Z", "+00:00")
                )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="sinceはISO日時またはcalibration_switchを指定してください"
                )

    if since_dt is not None and hasattr(models.Purchase, "created_at"):
        query = query.filter(models.Purchase.created_at >= since_dt)
        since_resolved = since_dt.isoformat()

    purchases = query.all()

    def empty_stats():
        return {
            "bet_count": 0,
            "hit_count": 0,
            "stake_total": 0.0,
            "predicted_return_from_stored_ev": 0.0,
            "predicted_return_from_prob_odds": 0.0,
            "actual_return": 0.0,
            "stored_ev_predicted_roi_pct": None,
            "prob_odds_predicted_roi_pct": None,
            "actual_roi_pct": None,
            "predicted_avg_prob_pct": None,
            "actual_hit_rate_pct": None,
            "avg_odds": None,
            "probability_sum_expected_hits": 0.0,
            "probability_sum_actual_hits": 0,
            "probability_gap_hits": None,
            "odds_available_count": 0,
            "stored_ev_available_count": 0,
        }

    def finalize(rows):
        s = empty_stats()

        if not rows:
            return s

        stake_total = sum(r["stake"] for r in rows)
        hit_count = sum(1 for r in rows if r["won"])
        actual_return = sum(r["actual_return"] for r in rows)

        stored_ev_return = sum(
            r["stake"] * (1.0 + r["ev_pct"] / 100.0)
            for r in rows
            if r["ev_pct"] is not None
        )

        prob_odds_return = sum(
            r["stake"] * r["prob"] * r["odds"]
            for r in rows
            if r["prob"] is not None and r["odds"] is not None and r["odds"] > 0
        )

        probs = [r["prob"] for r in rows if r["prob"] is not None]
        odds = [r["odds"] for r in rows if r["odds"] is not None and r["odds"] > 0]

        s.update({
            "bet_count": len(rows),
            "hit_count": hit_count,
            "stake_total": round(stake_total, 2),
            "predicted_return_from_stored_ev": round(stored_ev_return, 2),
            "predicted_return_from_prob_odds": round(prob_odds_return, 2),
            "actual_return": round(actual_return, 2),

            "stored_ev_predicted_roi_pct": (
                round(stored_ev_return / stake_total * 100.0, 4)
                if stake_total > 0 else None
            ),

            "prob_odds_predicted_roi_pct": (
                round(prob_odds_return / stake_total * 100.0, 4)
                if stake_total > 0 else None
            ),

            "actual_roi_pct": (
                round(actual_return / stake_total * 100.0, 4)
                if stake_total > 0 else None
            ),

            "predicted_avg_prob_pct": (
                round(sum(probs) / len(probs) * 100.0, 4)
                if probs else None
            ),

            "actual_hit_rate_pct": (
                round(hit_count / len(rows) * 100.0, 4)
                if rows else None
            ),

            "avg_odds": (
                round(sum(odds) / len(odds), 4)
                if odds else None
            ),

            "probability_sum_expected_hits": (
                round(sum(probs), 4)
                if probs else 0.0
            ),

            "probability_sum_actual_hits": hit_count,

            "probability_gap_hits": (
                round(hit_count - sum(probs), 4)
                if probs else None
            ),

            "odds_available_count": len(odds),

            "stored_ev_available_count": sum(
                1 for r in rows if r["ev_pct"] is not None
            ),
        })

        return s

    def odds_band(odds):
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
        return "300倍以上"

    def ev_band(ev):
        if ev is None:
            return "不明"
        if ev < 0:
            return "EVマイナス"
        if ev < 20:
            return "0-20%"
        if ev < 50:
            return "20-50%"
        if ev < 100:
            return "50-100%"
        if ev < 300:
            return "100-300%"
        return "300%以上"

    rows = []

    for p in purchases:
        stake = float(p.stake_amount or 0.0)

        prob = getattr(p, "win_prob_at_purchase", None)
        if prob is not None:
            try:
                prob = float(prob)
            except (TypeError, ValueError):
                prob = None

        odds = getattr(p, "odds_at_purchase", None)
        if odds is not None:
            try:
                odds = float(odds)
            except (TypeError, ValueError):
                odds = None

        ev_pct = getattr(p, "ev_pct_at_purchase", None)
        if ev_pct is not None:
            try:
                ev_pct = float(ev_pct)
            except (TypeError, ValueError):
                ev_pct = None

        payout = getattr(p, "payout_amount", None)
        try:
            actual_return = float(payout or 0.0)
        except (TypeError, ValueError):
            actual_return = 0.0

        won = p.result == "win"

        rows.append({
            "purchase_id": p.id,
            "race_id": p.race_id,
            "bet_type": p.bet_type,
            "stake": stake,
            "prob": prob,
            "odds": odds,
            "ev_pct": ev_pct,
            "actual_return": actual_return,
            "won": won,
        })

    by_odds = {}
    for band in [
        "1-5倍",
        "5-10倍",
        "10-30倍",
        "30-100倍",
        "100-300倍",
        "300倍以上",
        "不明",
    ]:
        group = [r for r in rows if odds_band(r["odds"]) == band]
        by_odds[band] = finalize(group)

    by_ev = {}
    for band in [
        "EVマイナス",
        "0-20%",
        "20-50%",
        "50-100%",
        "100-300%",
        "300%以上",
        "不明",
    ]:
        group = [r for r in rows if ev_band(r["ev_pct"]) == band]
        by_ev[band] = finalize(group)

    by_bet_type = {}
    for bet_type in sorted({r["bet_type"] for r in rows if r["bet_type"]}):
        group = [r for r in rows if r["bet_type"] == bet_type]
        by_bet_type[bet_type] = finalize(group)

    overall = finalize(rows)

    consistency = {
        "stored_ev_vs_prob_odds_return_gap": (
            round(
                overall["predicted_return_from_stored_ev"]
                - overall["predicted_return_from_prob_odds"],
                2
            )
        ),

        "prob_odds_vs_actual_return_gap": (
            round(
                overall["predicted_return_from_prob_odds"]
                - overall["actual_return"],
                2
            )
        ),

        "stored_ev_vs_actual_return_gap": (
            round(
                overall["predicted_return_from_stored_ev"]
                - overall["actual_return"],
                2
            )
        ),
    }

    return {
        "purpose": (
            "同一Purchase集合で予測払戻と実際払戻を直接比較する診断"
        ),
        "since": since,
        "since_resolved": since_resolved,
        "overall": overall,
        "by_odds_band": by_odds,
        "by_ev_band": by_ev,
        "by_bet_type": by_bet_type,
        "consistency": consistency,
        "interpretation": {
            "stored_ev": (
                "保存済みev_pct_at_purchaseから逆算した予測払戻"
            ),
            "prob_times_odds": (
                "win_prob_at_purchase × odds_at_purchase × stakeの合計"
            ),
            "actual": (
                "確定済みpayout_amountの合計"
            ),
        },
    }

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


@router.get("/order-accuracy")
def order_accuracy(stages: str = "S級特秀,S級選抜,S級準決勝,S級決勝", db: Session = Depends(get_db)):
    """
    「1着の予想」だけでなく「2着・3着の予想」がどれくらい当たっているかを、
    S級上位ステージとそれ以外に分けて比較する診断用エンドポイント
    (のんの要望により追加)。
    car-pick-accuracyで判明した「AIは1着予想は得意(むしろ弱気)」という結果を受けて、
    「車券が外れ続けているのは2着・3着(着順)の読みに原因があるのでは」という
    仮説を検証する。AIの予想確率が高い順に3台選び、実際の1〜3着と順位ごとに突き合わせる。
    """
    target_stages = {s.strip() for s in stages.split(",") if s.strip()}
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    def analyze_group(races_group):
        n = len(races_group)
        if n == 0:
            return None
        pos1_ok = pos2_ok = pos3_ok = 0
        top3_set_ok = 0
        exact_order_ok = 0
        pos2_given_pos1_ok_n = pos2_given_pos1_ok = 0
        pos3_given_pos12_ok_n = pos3_given_pos12_ok = 0
        for race, ranked, actual_groups, actual_top3 in races_group:
            predicted = [e.car_number for e in ranked[:3]]
            actual_pos = [g[0] if len(g) == 1 else None for g in actual_groups[:3]]  # 同着はNone(位置判定不可)扱い
            p1_ok = len(predicted) > 0 and actual_pos[0] is not None and predicted[0] == actual_pos[0]
            p2_ok = len(predicted) > 1 and len(actual_pos) > 1 and actual_pos[1] is not None and predicted[1] == actual_pos[1]
            p3_ok = len(predicted) > 2 and len(actual_pos) > 2 and actual_pos[2] is not None and predicted[2] == actual_pos[2]
            pos1_ok += p1_ok
            pos2_ok += p2_ok
            pos3_ok += p3_ok
            if set(predicted) == actual_top3:
                top3_set_ok += 1
            if predicted == actual_pos and None not in actual_pos:
                exact_order_ok += 1
            if p1_ok:
                pos2_given_pos1_ok_n += 1
                pos2_given_pos1_ok += p2_ok
            if p1_ok and p2_ok:
                pos3_given_pos12_ok_n += 1
                pos3_given_pos12_ok += p3_ok
        return {
            "n_races": n,
            "pos1_accuracy_pct": round(pos1_ok / n * 100, 1),
            "pos2_accuracy_pct": round(pos2_ok / n * 100, 1),
            "pos3_accuracy_pct": round(pos3_ok / n * 100, 1),
            "top3_set_accuracy_pct": round(top3_set_ok / n * 100, 1),
            "exact_order_accuracy_pct": round(exact_order_ok / n * 100, 1),
            "pos2_accuracy_given_pos1_correct_pct": (
                round(pos2_given_pos1_ok / pos2_given_pos1_ok_n * 100, 1) if pos2_given_pos1_ok_n else None
            ),
            "pos3_accuracy_given_pos12_correct_pct": (
                round(pos3_given_pos12_ok / pos3_given_pos12_ok_n * 100, 1) if pos3_given_pos12_ok_n else None
            ),
        }

    stage_group, other_group = [], []
    for race in races:
        ranked = sorted(
            (e for e in race.entries if e.blended_win_prob is not None),
            key=lambda e: -e.blended_win_prob,
        )
        if len(ranked) < 3:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except (ValueError, IndexError):
            continue
        if len(parsed["groups"]) < 3:
            continue
        item = (race, ranked, parsed["groups"], parsed["top3_set"])
        if race.race_stage in target_stages:
            stage_group.append(item)
        else:
            other_group.append(item)

    return {
        "target_stages": sorted(target_stages),
        "S級上位": analyze_group(stage_group),
        "それ以外": analyze_group(other_group),
    }


@router.get("/stage-diagnostic")
def stage_diagnostic(stages: str = "S級特秀,S級選抜,S級準決勝,S級決勝", db: Session = Depends(get_db)):
    """
    指定したレースステージ(既定はS級上位4ステージ)に絞って、レース単位で
    「AIの本命」と「tipstar勝率の本命」がそれぞれ勝ったか・一致していたかを
    比較する診断用エンドポイント(のんの要望により追加)。
    tipstarはAIとは独立に集計されたアプリ側の勝率で、市場のオッズそのものでは
    ないが「大衆の見立て」に近い参考値として使う(市場確率をAIの予想ロジックに
    混ぜるのとは別の話で、ここでは検証・原因分析にのみ使う)。
    目的: 「S級上位はAI固有の弱点なのか、それともこのクラス自体が元々荒れやすく
    tipstarの本命も同じように飛んでいるだけなのか」を切り分けるための材料集め。
    """
    target_stages = {s.strip() for s in stages.split(",") if s.strip()}
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .filter(models.Race.race_stage.in_(target_stages))
        .options(joinedload(models.Race.entries))
        .all()
    )

    items = []
    for race in races:
        entries = race.entries
        ai_candidates = [e for e in entries if e.blended_win_prob is not None]
        tipstar_candidates = [e for e in entries if e.app_win_rate is not None]
        if not ai_candidates:
            continue
        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except (ValueError, IndexError):
            continue
        if not parsed["groups"]:
            continue
        first_group = parsed["groups"][0]

        ai_pick = max(ai_candidates, key=lambda e: e.blended_win_prob)
        tipstar_pick = max(tipstar_candidates, key=lambda e: e.app_win_rate) if tipstar_candidates else None

        line_sizes = sorted((len(g) for g in (race.lines_data or [])), reverse=True)

        items.append({
            "race_id": race.id,
            "venue_name": race.venue_name,
            "race_number": race.race_number,
            "race_stage": race.race_stage,
            "actual_result": race.actual_result,
            "line_sizes": line_sizes,  # 例: [3,2,1,1] = 3車ラインが最大
            "ai_pick_car_number": ai_pick.car_number,
            "ai_pick_predicted_win_prob_pct": round(ai_pick.blended_win_prob * 100, 2),
            "ai_pick_won": ai_pick.car_number in first_group,
            "ai_pick_in_top3": ai_pick.car_number in parsed["top3_set"],
            "tipstar_pick_car_number": tipstar_pick.car_number if tipstar_pick else None,
            "tipstar_pick_win_rate_pct": tipstar_pick.app_win_rate if tipstar_pick else None,
            "tipstar_pick_won": (tipstar_pick.car_number in first_group) if tipstar_pick else None,
            "ai_agrees_with_tipstar": (
                ai_pick.car_number == tipstar_pick.car_number if tipstar_pick else None
            ),
        })

    if not items:
        return {"message": "対象ステージで着順確定済み・AI推定済みのレースがまだありません", "target_stages": sorted(target_stages)}

    n = len(items)
    ai_win = sum(1 for it in items if it["ai_pick_won"])
    ai_top3 = sum(1 for it in items if it["ai_pick_in_top3"])
    with_tipstar = [it for it in items if it["tipstar_pick_car_number"] is not None]
    n_tipstar = len(with_tipstar)
    tipstar_win = sum(1 for it in with_tipstar if it["tipstar_pick_won"])
    agree_items = [it for it in with_tipstar if it["ai_agrees_with_tipstar"]]
    disagree_items = [it for it in with_tipstar if it["ai_agrees_with_tipstar"] is False]

    def _summ(group):
        if not group:
            return None
        gn = len(group)
        return {
            "n": gn,
            "ai_win_rate_pct": round(sum(1 for it in group if it["ai_pick_won"]) / gn * 100, 1),
        }

    return {
        "target_stages": sorted(target_stages),
        "n_races": n,
        "ai_pick_win_rate_pct": round(ai_win / n * 100, 1),
        "ai_pick_top3_rate_pct": round(ai_top3 / n * 100, 1),
        "avg_ai_predicted_win_prob_pct": round(sum(it["ai_pick_predicted_win_prob_pct"] for it in items) / n, 2),
        "tipstar_comparison": {
            "n_with_tipstar_data": n_tipstar,
            "tipstar_pick_win_rate_pct": round(tipstar_win / n_tipstar * 100, 1) if n_tipstar else None,
            "ai_agrees_with_tipstar_rate_pct": round(len(agree_items) / n_tipstar * 100, 1) if n_tipstar else None,
            "when_agree": _summ(agree_items),
            "when_disagree": _summ(disagree_items),
        },
        "items": sorted(items, key=lambda x: -x["race_id"]),
    }


@router.get("/profit-concentration")
def profit_concentration(db: Session = Depends(get_db)):
    """
    利益がごく一部の大穴的中に偏っていないかを確認する
    (欠落していたエンドポイントをのんの指摘により復旧・再実装)。
    """
    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    if not purchases:
        return {"message": "まだ確定した購入履歴がありません"}

    hits = [p for p in purchases if p.result == "win"]
    misses = [p for p in purchases if p.result != "win"]
    total_stake = sum(p.stake_amount for p in purchases)
    total_payout = sum(p.payout_amount for p in purchases)

    gaiyou = {
        "総ベット数": len(purchases),
        "的中件数": len(hits),
        "不的中件数": len(misses),
        "総投資額": round(total_stake, 0),
        "総払戻": round(total_payout, 0),
        "総損益": round(total_payout - total_stake, 0),
    }

    hit_odds = [p.payout_amount / p.stake_amount for p in hits if p.stake_amount > 0]
    bunpu = {}
    if hit_odds:
        s = sorted(hit_odds)
        def pct(q):
            idx = min(len(s) - 1, int(len(s) * q))
            return round(s[idx], 2)
        bunpu = {
            "件数_オッズ判明": len(s),
            "中央値倍": pct(0.5),
            "平均倍": round(sum(s) / len(s), 2),
            "75%点倍": pct(0.75),
            "90%点倍": pct(0.9),
            "最大倍": round(s[-1], 2),
            "最小倍": round(s[0], 2),
        }

    def profit(p):
        return p.payout_amount - p.stake_amount

    hits_by_profit = sorted(hits, key=lambda p: -profit(p))
    total_profit = sum(profit(p) for p in purchases)
    total_hit_payout = sum(p.payout_amount for p in hits)

    def top_n_profit_share(n):
        if total_profit == 0:
            return None
        return round(sum(profit(p) for p in hits_by_profit[:n]) / total_profit * 100, 1)

    def top_n_payout_share(n):
        if total_hit_payout == 0:
            return None
        return round(sum(p.payout_amount for p in hits_by_profit[:n]) / total_hit_payout * 100, 1)

    big_hits = [p for p in hits if p.stake_amount > 0 and p.payout_amount / p.stake_amount >= 100]
    big_hits_profit_share = (
        round(sum(profit(p) for p in big_hits) / total_profit * 100, 1) if total_profit else None
    )

    # レース単位の黒字割合(のんの実機運用に合わせ、購入があったレースのみ対象)
    by_race = {}
    for p in purchases:
        by_race.setdefault(p.race_id, []).append(p)
    race_profits = {rid: sum(profit(p) for p in ps) for rid, ps in by_race.items()}
    profitable_races = [rid for rid, pf in race_profits.items() if pf > 0]
    races_sorted = sorted(race_profits.items(), key=lambda x: -x[1])
    top5_race_share = (
        round(sum(pf for _, pf in races_sorted[:5]) / total_profit * 100, 1) if total_profit else None
    )

    shuchuudo = {
        "的中上位5件が全体利益に占める割合%": top_n_profit_share(5),
        "的中上位10件が全体利益に占める割合%": top_n_profit_share(10),
        "的中上位10件が的中払戻に占める割合%": top_n_payout_share(10),
        "100倍以上の的中が全体利益に占める割合%": big_hits_profit_share,
        "黒字レース数": len(profitable_races),
        "対象レース数": len(race_profits),
        "黒字レース割合%": round(len(profitable_races) / len(race_profits) * 100, 1) if race_profits else None,
        "利益上位5レースが全体利益に占める割合%": top5_race_share,
    }

    hanteil = []
    if shuchuudo["的中上位10件が全体利益に占める割合%"] is not None:
        if shuchuudo["的中上位10件が全体利益に占める割合%"] >= 50:
            hanteil.append("的中上位10件だけで全体利益の半分以上を占めています。ごく一部の大穴的中に依存した収支である可能性が高いです。")
        else:
            hanteil.append("利益は特定の的中に極端には依存していません。")
    if shuchuudo["黒字レース割合%"] is not None and shuchuudo["黒字レース割合%"] < 30:
        hanteil.append("黒字レースの割合が3割未満です。多くのレースで負けながら、一部の大きな的中でカバーしている収支構造です。")

    # オッズ帯別
    band_defs = [("〜10倍", 0, 10), ("10〜30倍", 10, 30), ("30〜100倍", 30, 100), ("100倍以上", 100, float("inf"))]
    band_result = {}
    for label, lo, hi in band_defs:
        group = [p for p in hits if p.stake_amount > 0 and lo <= p.payout_amount / p.stake_amount < hi]
        pf = sum(profit(p) for p in group)
        band_result[label] = {
            "的中件数": len(group),
            "払戻合計": round(sum(p.payout_amount for p in group), 0),
            "利益合計": round(pf, 0),
            "利益が全体利益に占める割合%": round(pf / total_profit * 100, 1) if total_profit else None,
        }

    # 券種別(的中のみ)
    bet_result = {}
    for p in hits:
        b = bet_result.setdefault(p.bet_type, {"的中件数": 0, "払戻合計": 0.0, "利益合計": 0.0})
        b["的中件数"] += 1
        b["払戻合計"] += p.payout_amount
        b["利益合計"] += profit(p)
    for v in bet_result.values():
        v["払戻合計"] = round(v["払戻合計"], 0)
        v["利益合計"] = round(v["利益合計"], 0)

    # 想定勝率帯別(的中のみ)
    prob_result = {}
    for p in hits:
        name, _ = calc.get_prob_bucket(p.win_prob_at_purchase or 0)
        b = prob_result.setdefault(name, {"的中件数": 0, "払戻合計": 0.0, "利益合計": 0.0})
        b["的中件数"] += 1
        b["払戻合計"] += p.payout_amount
        b["利益合計"] += profit(p)
    for v in prob_result.values():
        v["払戻合計"] = round(v["払戻合計"], 0)
        v["利益合計"] = round(v["利益合計"], 0)

    tops = []
    for p in hits_by_profit[:20]:
        tops.append({
            "レースID": p.race_id,
            "券種": p.bet_type,
            "買い目": p.combination,
            "オッズ": round(p.payout_amount / p.stake_amount, 2) if p.stake_amount else None,
            "投資額": round(p.stake_amount, 0),
            "払戻": round(p.payout_amount, 0),
            "利益": round(profit(p), 0),
            "購入時想定勝率%": round(p.win_prob_at_purchase * 100, 2) if p.win_prob_at_purchase is not None else None,
        })

    return {
        "概要": gaiyou,
        "的中オッズの分布": bunpu,
        "集中度": shuchuudo,
        "判定": hanteil,
        "的中オッズ帯別": band_result,
        "的中の券種別": bet_result,
        "的中の想定勝率帯別": prob_result,
        "利益の大きい的中_上位": tops,
    }


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
    purchases_only = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    class _SkippedAsPurchase:
        __slots__ = (
            "race_id", "bet_type", "combination", "stake_amount", "payout_amount",
            "result", "win_prob_at_purchase", "ev_pct_at_purchase", "is_skipped_record",
            "odds_at_purchase", "final_odds",
        )
        def __init__(self, s):
            self.race_id = s.race_id
            self.bet_type = s.bet_type
            self.combination = s.combination
            self.stake_amount = 0.0
            self.payout_amount = 0.0
            self.result = s.actual_result
            self.win_prob_at_purchase = s.win_prob_estimated
            self.ev_pct_at_purchase = s.ev_pct_estimated
            self.is_skipped_record = True
            # 見送りはオッズ変動(投票時→最終)の追跡対象ではないため常にNone
            # (のんの実機運用で発覚したAttributeErrorを修正。以前はこの属性自体が
            # 無く、見送りを含む集計処理が軒並み500エラーになっていた)。
            self.odds_at_purchase = None
            self.final_odds = None
    skipped_eval = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.isnot(None))
        .all()
    )
    purchases = list(purchases_only) + [_SkippedAsPurchase(s) for s in skipped_eval]
    if not purchases:
        return {"message": "まだ確定した購入履歴がありません"}

    total_stake = sum(p.stake_amount for p in purchases_only)
    total_payout = sum(p.payout_amount for p in purchases_only)
    overall_expectancy_pct = ((total_payout - total_stake) / total_stake * 100) if total_stake else 0

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
                "ev_pct_sum": 0.0, "ev_pct_count": 0,
                "purchased_count": 0,
            })
            b["count"] += 1
            if p.result == "win":
                b["wins"] += 1
            # win_prob_at_purchase / ev_pct_at_purchase は、購入時点でのAI予想値
            # (実績ではなく「買った時、AIはどう見積もっていたか」のスナップショット)。
            if p.win_prob_at_purchase is not None:
                b["win_prob_sum"] += p.win_prob_at_purchase
                b["win_prob_count"] += 1
            # 見送り(SkippedBet)は実際にお金を賭けていない(stake=0固定)ため、
            # 的中率の検証には使うが、収支(実績金額)の集計には混ぜない
            # (のんの指摘により修正。以前は見送りのstake=0がそのまま平均に混ざり、
            # 見送りが大半を占める勝率帯[特に大穴]の「実績」が実態と無関係に0%表示に
            # なっていた)。
            if p.stake_amount > 0:
                b["purchased_count"] += 1
                b["stake"] += p.stake_amount
                b["payout"] += p.payout_amount
            # 「想定回収率」はAIが見積もった理論値であり、実際に賭けたかどうかに
            # 関係なく計算できる(のんの指摘により修正。以前は購入分の投資額で
            # 加重していたため、見送りしかない条件では計算不能=空欄になっていた)。
            # 予想精度の比較には使わない値なので、購入分とは違い単純平均でよい。
            if p.ev_pct_at_purchase is not None:
                b["ev_pct_sum"] += p.ev_pct_at_purchase
                b["ev_pct_count"] += 1
        out = {}
        for k, v in buckets.items():
            has_purchase = v["purchased_count"] > 0
            expectancy = ((v["payout"] - v["stake"]) / v["stake"] * 100) if has_purchase else None
            expected_win_rate_pct = (
                round(v["win_prob_sum"] / v["win_prob_count"] * 100, 1) if v["win_prob_count"] else None
            )
            # ev_pct_at_purchaseは「0%が損益分岐点」表現のため、+100して実績(roi_pct)と
            # 同じ「100%が損益分岐点」表現に揃える。実際に賭けたか否かに関係なく
            # 全件の単純平均を使う(のんの指摘により修正。予想精度の比較には使わない)。
            expected_roi_pct = (
                round(v["ev_pct_sum"] / v["ev_pct_count"] + 100, 2) if v["ev_pct_count"] else None
            )
            expected_profit = None
            out[k] = {
                "count": v["count"],
                "purchased_count": v["purchased_count"],
                "win_rate_pct": round(v["wins"] / v["count"] * 100, 1),
                "expected_win_rate_pct": expected_win_rate_pct,
                # roi_pct: 回収率(100%が損益分岐点)。expectancy_pct: 同じ値を「0%が損益分岐点」の表現にしたもの。
                # 実際に購入した件数が0件(見送りのみ)の場合はNone(集計不可)にする。
                "roi_pct": round(expectancy + 100, 2) if expectancy is not None else None,
                "expectancy_pct": round(expectancy, 2) if expectancy is not None else None,
                "profit": round(v["payout"] - v["stake"], 0) if has_purchase else None,
                "expected_roi_pct": expected_roi_pct,
                "expected_profit": expected_profit,
            }
        # 実績が高い順に並べ替える(見送りのみで実績算出不可のものは末尾に回す)
        return dict(sorted(
            out.items(),
            key=lambda item: (item[1]["expectancy_pct"] is None, -(item[1]["expectancy_pct"] or 0)),
        ))

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

    def _norm_leg(s):
        """集計表示時の脚質文字化け・略称を吸収(DB未修復でも画面を壊さない)。"""
        if not s:
            return s
        t = str(s)
        for bad, good in (
            ("йҖғ", "逃げ"), ("иҝҪ", "追込"), ("дёЎ", "両方"),
            ("逃", "逃げ"), ("追込", "追込"), ("追", "追込"), ("両", "両方"),
        ):
            if bad in t and bad != good:
                # 「逃」→「逃げ」は「逃げ」内の「逃」を二重化しないよう注意
                if bad == "逃" and "逃げ" in t:
                    continue
                if bad == "追" and "追込" in t:
                    continue
                if bad == "両" and "両方" in t:
                    continue
                t = t.replace(bad, good)
        return t

    def leg_style_bucket(p):
        cars = _combination_cars(p)
        if not cars:
            return "脚質情報なし"
        entries = [e for e in entries_by_race_id.get(p.race_id, []) if e.car_number in cars]
        styles = [_norm_leg(e.leg_style) for e in entries if e.leg_style]
        styles = [s for s in styles if s]
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
                    "purchased_count": v["purchased_count"],
                    "win_rate_pct": v["win_rate_pct"],
                    "expected_win_rate_pct": v["expected_win_rate_pct"],
                    "expectancy_pct": v["expectancy_pct"],
                    "expected_roi_pct": v["expected_roi_pct"],
                })
    # 見送りのみ(実績算出不可=expectancy_pct が None)の条件はランキングに含めない
    # (のんの指摘により修正。以前は0%扱いされ、大穴帯の見送りばかりが
    # 「不調な条件」の上位を占めてしまっていた)。
    ranking = [r for r in ranking if r["expectancy_pct"] is not None]
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
    overall_win_rate_pct = round(overall_win_count / len(purchases) * 100, 1) if purchases else 0.0

    # 資金管理シミュレーション用の勝率・オッズ。
    # 【バグ修正】以前は「全買い目の投資額加重平均オッズ」(外れ含む)をモンテカルロに
    # 渡していた。モデルは「的中率pでオッズO倍」なので、全件平均オッズ×的中率だと
    # 期待値が大きくマイナスになり、実績ROIと矛盾して長期でほぼ全破産になる。
    # 正しい組: O = (総払戻/総投資) / (的中数/総件数)  →  p×O = 実績の回収倍率。
    def _sim_params_for(subset):
        if not subset:
            return None
        n = len(subset)
        wins = [p for p in subset if p.result == "win"]
        n_win = len(wins)
        hit_pct = round(n_win / n * 100, 2)
        stake_sum = sum(p.stake_amount for p in subset)
        payout_sum = sum(p.payout_amount or 0 for p in subset)
        roi_mult = (payout_sum / stake_sum) if stake_sum > 0 else 0.0
        p = n_win / n
        odds_ev = round(roi_mult / p, 2) if p > 0 else None
        odds_on_wins = None
        wins_with_odds = [p for p in wins if p.odds_at_purchase is not None]
        w_stake = sum(p.stake_amount for p in wins_with_odds)
        if w_stake > 0:
            odds_on_wins = round(
                sum(p.stake_amount * p.odds_at_purchase for p in wins_with_odds) / w_stake, 2
            )
        all_with_odds = [p for p in subset if p.odds_at_purchase is not None]
        odds_all_bets = None
        if all_with_odds:
            a_stake = sum(p.stake_amount for p in all_with_odds)
            if a_stake > 0:
                odds_all_bets = round(
                    sum(p.stake_amount * p.odds_at_purchase for p in all_with_odds) / a_stake, 2
                )
        return {
            "win_rate_pct": hit_pct,
            "odds_for_sim": odds_ev,
            "odds_on_wins_weighted": odds_on_wins,
            "avg_odds_all_bets_weighted": odds_all_bets,
            "roi_pct": round(roi_mult * 100, 2),
            "n": n,
            "wins": n_win,
        }

    sim_overall = _sim_params_for(purchases)
    sim_by_bet_type = {}
    for bt in sorted({p.bet_type for p in purchases if p.bet_type}):
        params = _sim_params_for([p for p in purchases if p.bet_type == bt])
        if params:
            sim_by_bet_type[bt] = params

    # 後方互換: 自動入力の avg_odds_weighted は実績ROI整合の的中時倍率
    avg_odds_weighted = sim_overall["odds_for_sim"] if sim_overall else None

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
        "sim_overall": sim_overall,
        "sim_by_bet_type": sim_by_bet_type,
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
