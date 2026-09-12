from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_
from typing import Optional
from datetime import datetime, timedelta

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


def get_calibration_factors(db: Session, as_of_dt: Optional[datetime] = None) -> dict:
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
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.bet_type == "3連単")
        .all()
    )
    if as_of_dt is not None:
        race_times = {
            r.id: _race_event_dt(r)
            for r in db.query(models.Race).all()
        }
        purchases = [
            p for p in purchases
            if race_times.get(p.race_id) is not None
            and race_times[p.race_id] < as_of_dt
        ]
    skipped = (
        db.query(models.SkippedBet)
        .filter(models.SkippedBet.actual_result.isnot(None))
        .filter(models.SkippedBet.bet_type == "3連単")
        .all()
    )

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
import os as _os

# プロセス起動時刻・PID。race_plan/warm-calibrationのたびに毎回フル再計算に
# なっている件を調査するため、プロセスがリクエスト間で本当に同一かを確認する目印
# (Render無料プランのメモリ上限でプロセスが再起動している可能性を検証するため、
# のんの報告「キャッシュが効いていない(毎回47秒前後)」を受けて追加)。
_PROCESS_STARTED_AT = _time.time()
_PROCESS_PID = _os.getpid()

_retroactive_calibration_cache = {"computed_at": 0.0, "value": None}
_purchase_set_calibration_cache = {"computed_at": 0.0, "value": None}
RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS = 60 * 60  # 60分（replay中に再計算しない）


def _race_event_dt(race):
    """
    リプレイ時の時系列基準。
    post_time(発走予定時刻)を最優先し、無ければrace_date、created_atへフォールバック。
    as_of付き集計では時刻不明のレースを未来情報混入防止のため除外する。
    """
    return (
        getattr(race, "post_time", None)
        or getattr(race, "race_date", None)
        or getattr(race, "created_at", None)
    )


def _race_is_before_as_of(race, as_of_dt):
    if as_of_dt is None:
        return True
    event_dt = _race_event_dt(race)
    return event_dt is not None and event_dt < as_of_dt




def get_calibration_factors_retroactive(db: Session, use_cache: bool = True, as_of_dt: Optional[datetime] = None) -> dict:
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
    if use_cache and as_of_dt is None:
        cached = _retroactive_calibration_cache["value"]
        if cached is not None and (now - _retroactive_calibration_cache["computed_at"]) < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS:
            return cached

    # レースとオッズを一括取得（レースごとのN+1クエリを避ける）
    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )
    if as_of_dt is not None:
        races = [r for r in races if _race_is_before_as_of(r, as_of_dt)]
    from collections import defaultdict
    odds_by_race = defaultdict(list)
    race_ids = [r.id for r in races]
    if race_ids:
        # 大きすぎるIN句を避けるためチャンク
        CHUNK = 500
        for i in range(0, len(race_ids), CHUNK):
            chunk = race_ids[i:i + CHUNK]
            for o in db.query(models.Odds).filter(models.Odds.race_id.in_(chunk)).all():
                odds_by_race[o.race_id].append(o)

    records = []
    for race in races:
        win_probs = calc.build_win_probs_from_entries(race.entries)
        if not win_probs:
            continue
        odds_rows = odds_by_race.get(race.id) or []
        if not odds_rows:
            continue
        try:
            parsed_result = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue
        line_map, line_boost = calc.line_map_from_race(race)
        car_numbers_all = sorted(win_probs.keys())
        norm_mass = {}
        for arity in (2, 3):
            if len(car_numbers_all) >= arity:
                mass = calc.total_ordered_mass(
                    win_probs, car_numbers_all, arity, line_map=line_map, line_boost=line_boost,
                )
                norm_mass[arity] = mass if mass > 1e-9 else 1.0
            else:
                norm_mass[arity] = 1.0
        for o in odds_rows:
            if o.bet_type not in TARGET_BET_TYPES:
                continue
            try:
                cars = tuple(int(x) for x in o.combination.split("-"))
            except (ValueError, AttributeError):
                continue
            try:
                prob_raw = calc.estimate_prob_for_bet(
                    win_probs, o.bet_type, cars, line_map=line_map, line_boost=line_boost,
                )
                arity = calc.BET_TYPE_ARITY.get(o.bet_type)
                if arity in norm_mass:
                    prob_raw = prob_raw / norm_mass[arity]
            except Exception:
                continue
            won = calc.judge_purchase_result(o.bet_type, o.combination, parsed_result)
            records.append((prob_raw, won, "retroactive", o.bet_type))

    result = _compute_calibration_factors_from_records(records)
    if as_of_dt is None:
        _retroactive_calibration_cache["value"] = result
        _retroactive_calibration_cache["computed_at"] = now
    return result



def get_purchase_set_calibration_factors(db: Session, use_cache: bool = True, as_of_dt: Optional[datetime] = None) -> dict:
    """
    「実際に購入した集合」だけでの予測確率 vs 実績的中から追加補正係数を作る。

    通常の帯校正（全候補・見送り込み）後も、購入群では予測が約2倍楽観的なまま
    残ることが診断で判明した。選別後の条件付き分布に合わせた第2段校正。

    factor = actual_hit_rate / predicted_avg_prob（shrink付き）
    """
    global _purchase_set_calibration_cache
    now = _time.time()
    if use_cache and as_of_dt is None:
        cached = _purchase_set_calibration_cache.get("value")
        if cached is not None and (now - _purchase_set_calibration_cache.get("computed_at", 0)) < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS:
            return cached

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result.in_(("win", "lose")))
        .filter(models.Purchase.win_prob_at_purchase.isnot(None))
        .all()
    )
    if as_of_dt is not None:
        race_times = {
            r.id: _race_event_dt(r)
            for r in db.query(models.Race).all()
        }
        purchases = [
            p for p in purchases
            if race_times.get(p.race_id) is not None
            and race_times[p.race_id] < as_of_dt
        ]

    def _band(odds):
        return odds_band_label(odds)

    def _factor(rows):
        n = len(rows)
        if n == 0:
            return None
        wins = sum(1 for _, won in rows if won)
        pred = sum(p for p, _ in rows) / n
        act = wins / n
        if pred <= 1e-12:
            return {
                "n": n,
                "wins": wins,
                "predicted_avg_pct": round(pred * 100, 4),
                "actual_hit_rate_pct": round(act * 100, 4),
                "factor": 1.0,
            }
        raw = act / pred
        # 2026-09-07修正(のんの指摘により変更): 信頼度の基準を「生の試行数n」から
        # 「期待的中数(pred×n)」に変更した。
        # 旧方式はnが80件を超えると一律floor=0.12まで縮小していたが、3連単のような
        # 低確率高配当の券種はn=85でも期待的中数は1件未満で、実際の的中/外れの
        # ブレだけで係数が0.12まで暴れる事故が発生した(実際は黒字の3連単が
        # 大赤字と誤判定された)。二項分布で比率が意味を持つには「試行回数」ではなく
        # 「期待される事象の発生回数」がある程度必要という統計的に妥当な基準に変更し、
        # 低確率帯ほど多くの試行数が要求されるようにした。
        expected_wins = pred * n
        required_expected_wins = 15.0
        shrink = min(1.0, expected_wins / required_expected_wins)
        factor = 1.0 + shrink * (raw - 1.0)
        # 大規模サンプル(期待的中数ベース)で大きく楽観が残る帯は下限を下げて寄せる
        # (2車単・10-30倍帯など。2026-09-04の6927件診断で確認。pred~15%×6927件で
        # 期待的中数は1000件超のため、旧n基準・新expected_wins基準どちらでも
        # floorが効く対象であることに変わりはない)。
        floor = 0.08 if expected_wins >= 20 and raw < 0.5 else 0.15 if expected_wins >= 10 else 0.3
        factor = max(floor, min(1.5, factor))
        return {
            "n": n,
            "wins": wins,
            "predicted_avg_pct": round(pred * 100, 4),
            "actual_hit_rate_pct": round(act * 100, 4),
            "raw_ratio": round(raw, 4),
            "expected_wins": round(expected_wins, 2),
            "factor": round(factor, 4),
        }

    all_rows = []
    by_bt = {}
    by_odds = {}
    by_bt_odds = {}

    for p in purchases:
        # 第2段は「帯校正後に保存された win_prob_at_purchase」を基準に残差を見る
        # （rawだと二重に強くなりすぎる）
        prob = float(p.win_prob_at_purchase)
        won = p.result == "win"
        odds = float(p.odds_at_purchase) if p.odds_at_purchase else None
        all_rows.append((prob, won))
        by_bt.setdefault(p.bet_type, []).append((prob, won))
        band = _band(odds)
        by_odds.setdefault(band, []).append((prob, won))
        by_bt_odds.setdefault(p.bet_type, {}).setdefault(band, []).append((prob, won))

    result = {
        "overall": _factor(all_rows),
        "by_bet_type": {k: _factor(v) for k, v in by_bt.items()},
        "by_odds_band": {k: _factor(v) for k, v in by_odds.items()},
        "by_bet_type_odds_band": {
            bt: {band: _factor(rows) for band, rows in bands.items()}
            for bt, bands in by_bt_odds.items()
        },
        "note": (
            "実購入のみ。win_prob_at_purchase（既存校正後）に対する残差係数。"
            "race-planでは帯校正の後にこの係数を掛ける。"
        ),
    }
    if as_of_dt is None:
        _purchase_set_calibration_cache["value"] = result
        _purchase_set_calibration_cache["computed_at"] = now
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

        # 2026-09-07追加(ChatGPT分析項目5対応): 実績的中率の95%信頼区間
        # (Wilson score interval)。件数が少ない帯ほど区間が広くなるため、
        # 「回収率が高い」という見た目だけで小サンプルの帯を過信しない材料にする。
        win_rate_ci95 = None
        if count > 0:
            lo, hi = calc.wilson_score_interval(wins, count)
            win_rate_ci95 = {"ci95_low_pct": round(lo * 100, 2), "ci95_high_pct": round(hi * 100, 2)}

        result[name] = {
            "sample_count": count,
            "purchase_count": purchase_count,
            "skipped_count": skipped_count,
            "required_sample_count": required,
            "is_reliable": is_reliable,
            "actual_win_rate_pct": round(actual_win_rate * 100, 2) if actual_win_rate is not None else None,
            "actual_win_rate_ci95": win_rate_ci95,
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

_stage_expectancy_cache = {"computed_at": 0.0, "value": None}
_bet_type_expectancy_cache = {"computed_at": 0.0, "value": None}
_bet_type_odds_band_expectancy_cache = {"computed_at": 0.0, "value": None}
_high_odds_residual_cache = {"computed_at": 0.0, "value": None}


def get_stage_expectancy_map(db: Session, min_samples: int = 50, use_cache: bool = True, as_of_dt: Optional[datetime] = None) -> dict:
    """
    レースステージごとの実績収支率(回収率-100)を返す。
    サンプルが min_samples 未満のステージは含めない。
    戻り値: {stage_name: {"n": int, "expectancy_pct": float, "win_rate_pct": float}}

    2026-09-07修正(のんの指摘により変更):
      1. 現行の投票基準(CALIBRATION_SWITCH_AT)より前のPurchaseを除外するようにした。
         以前は全期間を対象にしており、旧ロジック時代の実績がいつまでもゲートに残っていた。
      2. (撤回) 実購入に見送り(SkippedBet)を仮想投資額100円で合算する変更を
         一度入れたが、見送りには「確率が低すぎて評価しただけの候補」が大量に
         含まれ、これを全部買った前提の平均を取ると実態と乖離した数値になる
         (3連単が実際は黒字なのに、見送り合算後は大赤字と判定される事故が発生)。
         実購入だけを対象にする方式に戻した。
    """
    now = _time.time()
    if use_cache and as_of_dt is None:
        cached = _stage_expectancy_cache["value"]
        if cached is not None and (now - _stage_expectancy_cache["computed_at"]) < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS:
            return cached

    rows = (
        db.query(models.Purchase, models.Race.race_stage)
        .join(models.Race, models.Race.id == models.Purchase.race_id)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.purchased_at >= CALIBRATION_SWITCH_AT)
        .filter(models.Race.race_stage.isnot(None))
        .all()
    )
    if as_of_dt is not None:
        rows = [
            (p, stage)
            for p, stage in rows
            if _race_is_before_as_of(
                db.query(models.Race).get(p.race_id),
                as_of_dt,
            )
        ]
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
    if as_of_dt is None:
        _stage_expectancy_cache["value"] = out
        _stage_expectancy_cache["computed_at"] = now
    return out


def get_bet_type_expectancy_map(db: Session, min_samples: int = 50, use_cache: bool = True) -> dict:
    """
    券種ごとの実績収支率。get_stage_expectancy_mapと同じ理由でキャッシュを追加。

    2026-09-07修正: get_stage_expectancy_mapと同じ理由で
      1. CALIBRATION_SWITCH_AT以降のPurchaseだけを対象にする。
      2. (撤回) 見送り合算は事故のため撤回。get_stage_expectancy_mapの
         コメント参照。実購入だけを対象にする。
    """
    now = _time.time()
    if use_cache:
        cached = _bet_type_expectancy_cache["value"]
        if cached is not None and (now - _bet_type_expectancy_cache["computed_at"]) < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS:
            return cached

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.purchased_at >= CALIBRATION_SWITCH_AT)
        .all()
    )
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
    _bet_type_expectancy_cache["value"] = out
    _bet_type_expectancy_cache["computed_at"] = now
    return out


def odds_band_label(odds) -> str:
    """実績ゲート・PVA共通のオッズ帯ラベル（細分化版）。"""
    if odds is None:
        return "不明"
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return "不明"
    if o <= 0:
        return "不明"
    if o < 5:
        return "1-5倍"
    if o < 10:
        return "5-10倍"
    if o < 30:
        return "10-30倍"
    if o < 100:
        return "30-100倍"
    if o < 300:
        return "100-300倍"
    if o < 1000:
        return "300-1000倍"
    if o < 3000:
        return "1000-3000倍"
    return "3000倍以上"


def get_bet_type_odds_band_expectancy_map(
    db: Session, min_samples: int = 30, use_cache: bool = True
) -> dict:
    """
    券種×オッズ帯ごとの実績収支率。
    実績ゲートを「券種まるごと」ではなく「3連単×1000-3000倍」など細かく切るために使う。
    キーは "{bet_type}|{odds_band}"。値は expectancy_pct / n / win_rate_pct。

    2026-09-07修正: get_stage_expectancy_map/get_bet_type_expectancy_mapと同じ理由で
    CALIBRATION_SWITCH_AT以降のPurchaseだけを対象にする。
    (撤回) 見送り合算も一度実装したが、get_stage_expectancy_mapと同じ理由
    (評価しただけの低確率候補が母数を歪める事故)で撤回し、実購入のみに戻した。
    """
    now = _time.time()
    if use_cache:
        cached = _bet_type_odds_band_expectancy_cache["value"]
        if (
            cached is not None
            and (now - _bet_type_odds_band_expectancy_cache["computed_at"])
            < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS
        ):
            return cached

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.purchased_at >= CALIBRATION_SWITCH_AT)
        .all()
    )
    buckets = {}
    for p in purchases:
        # odds_at_purchase が空の古い行でも帯を推定できるよう補完する
        # （欠けると券種×帯ゲートが発火せず 1000-3000倍が残る原因になる）
        odds = p.odds_at_purchase
        if odds is None or odds <= 0:
            if p.final_odds is not None and p.final_odds > 0:
                odds = p.final_odds
            elif (
                p.result == "win"
                and p.stake_amount
                and p.payout_amount
                and p.stake_amount > 0
            ):
                odds = p.payout_amount / p.stake_amount
        band = odds_band_label(odds)
        if band == "不明":
            continue
        key = f"{p.bet_type}|{band}"
        b = buckets.setdefault(key, {"stake": 0.0, "payout": 0.0, "n": 0, "wins": 0,
                                       "bet_type": p.bet_type, "odds_band": band})
        b["stake"] += p.stake_amount or 0
        b["payout"] += p.payout_amount or 0
        b["n"] += 1
        if p.result == "win":
            b["wins"] += 1

    out = {}
    for key, b in buckets.items():
        if b["n"] < min_samples or b["stake"] <= 0:
            continue
        exp = (b["payout"] - b["stake"]) / b["stake"] * 100
        out[key] = {
            "bet_type": b["bet_type"],
            "odds_band": b["odds_band"],
            "n": b["n"],
            "expectancy_pct": round(exp, 2),
            "win_rate_pct": round(b["wins"] / b["n"] * 100, 2),
        }
    _bet_type_odds_band_expectancy_cache["value"] = out
    _bet_type_odds_band_expectancy_cache["computed_at"] = now
    return out




HIGH_ODDS_BANDS = ("300-1000倍", "1000-3000倍", "3000倍以上")


def get_high_odds_residual_factors(db: Session, use_cache: bool = True, as_of_dt: Optional[datetime] = None) -> dict:
    """
    高オッズ帯(300-1000 / 1000-3000 / 3000以上)専用の的中率残差係数。

    全期間ROIがプラスでも、予測的中率が実績より高い帯は確率を縮める(方針B)。
    2026-09-05: 300-1000倍も直近で勝率過大傾向のため対象に追加。
    買い目の禁止ではなく est_prob に係数を掛けるだけ。

    戻り値:
      {
        "by_odds_band": {band: {n, predicted_avg_pct, actual_hit_rate_pct, factor}},
        "by_bet_type_odds_band": {bet_type: {band: {...}}},
      }
    """
    now = _time.time()
    if use_cache and as_of_dt is None:
        cached = _high_odds_residual_cache["value"]
        if (
            cached is not None
            and (now - _high_odds_residual_cache["computed_at"])
            < RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS
        ):
            return cached

    purchases = db.query(models.Purchase).filter(models.Purchase.result != "pending").all()
    by_band = {}
    by_bt_band = {}

    def _resolve_odds(p):
        odds = p.odds_at_purchase
        if odds is None or odds <= 0:
            if p.final_odds is not None and p.final_odds > 0:
                odds = p.final_odds
            elif (
                p.result == "win"
                and p.stake_amount
                and p.payout_amount
                and p.stake_amount > 0
            ):
                odds = p.payout_amount / p.stake_amount
        return odds

    for p in purchases:
        odds = _resolve_odds(p)
        band = odds_band_label(odds)
        if band not in HIGH_ODDS_BANDS:
            continue
        prob = p.win_prob_at_purchase
        if prob is None:
            prob = p.win_prob_raw
        if prob is None or prob <= 0:
            continue
        won = p.result == "win"
        by_band.setdefault(band, []).append((float(prob), won))
        by_bt_band.setdefault(p.bet_type, {}).setdefault(band, []).append((float(prob), won))

    def _factor(rows, min_n):
        n = len(rows)
        if n < min_n:
            return None
        wins = sum(1 for _, w in rows if w)
        pred = sum(pr for pr, _ in rows) / n
        act = wins / n
        if pred <= 1e-12:
            return {
                "n": n,
                "wins": wins,
                "predicted_avg_pct": round(pred * 100, 4),
                "actual_hit_rate_pct": round(act * 100, 4),
                "factor": 1.0,
            }
        raw = act / pred
        # サンプルが増えるほど生比率に寄せる
        required = 40
        shrink = min(1.0, n / required)
        factor = 1.0 + shrink * (raw - 1.0)
        # 高オッズは下限を低めに（過大予測を強く戻す）
        floor = 0.08 if n >= 50 else 0.12
        factor = max(floor, min(1.5, factor))
        return {
            "n": n,
            "wins": wins,
            "predicted_avg_pct": round(pred * 100, 4),
            "actual_hit_rate_pct": round(act * 100, 4),
            "raw_ratio": round(raw, 4),
            "factor": round(factor, 4),
        }

    out_band = {}
    for band, rows in by_band.items():
        info = _factor(rows, min_n=30)
        if info:
            out_band[band] = info

    out_bt = {}
    for bt, bands in by_bt_band.items():
        cell = {}
        for band, rows in bands.items():
            info = _factor(rows, min_n=20)
            if info:
                cell[band] = info
        if cell:
            out_bt[bt] = cell

    out = {"by_odds_band": out_band, "by_bet_type_odds_band": out_bt}
    if as_of_dt is None:
        _high_odds_residual_cache["value"] = out
        _high_odds_residual_cache["computed_at"] = now
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




@router.post("/warm-calibration")
def warm_calibration(db: Session = Depends(get_db)):
    """
    遡及校正・購入集合校正・ステージ/券種ゲート集計を先に計算してキャッシュする。
    race-plan / replay の初回が Render のタイムアウト(約150s)に当たらないようにする。

    2026-09-03: race-planが1レースあたり約50秒かかる件を調査した結果、
    ステージ/券種ゲート用の集計(get_stage_expectancy_map/get_bet_type_expectancy_map)
    がPurchase全件を毎回フルスキャンしておりキャッシュされていなかったことが判明。
    校正係数と同様にキャッシュを追加した上で、ここでもウォームアップする。
    """
    t0 = _time.time()
    retro = get_calibration_factors_retroactive(db, use_cache=False)
    t1 = _time.time()
    purchase_set = get_purchase_set_calibration_factors(db, use_cache=False)
    t2 = _time.time()
    stage_exp = get_stage_expectancy_map(db, min_samples=50, use_cache=False)
    t3 = _time.time()
    bet_type_exp = get_bet_type_expectancy_map(db, min_samples=50, use_cache=False)
    t4 = _time.time()
    bt_odds_exp = get_bet_type_odds_band_expectancy_map(db, min_samples=30, use_cache=False)
    t5 = _time.time()
    high_odds = get_high_odds_residual_factors(db, use_cache=False)
    t6 = _time.time()
    overall = (retro or {}).get("overall") or {}
    ps = (purchase_set or {}).get("overall") or {}
    return {
        "ok": True,
        "retroactive_seconds": round(t1 - t0, 2),
        "purchase_set_seconds": round(t2 - t1, 2),
        "stage_expectancy_seconds": round(t3 - t2, 2),
        "bet_type_expectancy_seconds": round(t4 - t3, 2),
        "bet_type_odds_band_expectancy_seconds": round(t5 - t4, 2),
        "high_odds_residual_seconds": round(t6 - t5, 2),
        "total_seconds": round(t6 - t0, 2),
        "retroactive_overall_factor": overall.get("calibration_factor"),
        "retroactive_sample_count": overall.get("sample_count"),
        "purchase_set_factor": ps.get("factor"),
        "purchase_set_n": ps.get("n"),
        "stage_expectancy_stage_count": len(stage_exp),
        "bet_type_expectancy_count": len(bet_type_exp),
        "bet_type_odds_band_expectancy_count": len(bt_odds_exp),
        "bet_type_expectancy": bet_type_exp,
        "bet_type_odds_band_expectancy": bt_odds_exp,
        "bet_type_odds_band_negative": {
            k: v for k, v in sorted(bt_odds_exp.items())
            if (v.get("expectancy_pct") is not None and v["expectancy_pct"] < 0)
        },
        "high_odds_residual": high_odds,
        "cache_ttl_seconds": RETROACTIVE_CALIBRATION_CACHE_TTL_SECONDS,
        "process_pid": _PROCESS_PID,
        "process_uptime_seconds": round(_time.time() - _PROCESS_STARTED_AT, 1),
        "message": "キャッシュ済み。続けて replay / race-plan を実行してください。",
    }


@router.get("/investment-readiness")
def investment_readiness(since: Optional[str] = "calibration_switch", db: Session = Depends(get_db)):
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
        .filter(
            models.Purchase.result != "pending",
            models.Purchase.bet_type == "3連単",
            models.Purchase.win_prob_at_purchase.isnot(None),
        )
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
    bucket_only = buckets
    if isinstance(buckets, dict):
        factor_overall = buckets.get("overall")
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
            models.Purchase.bet_type == "3連単",
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
        "message": (
            "【見方】主に「補正の効き」と「直近3日/7日」を見てください。"
            "全期間の1本の乖離は母数が大きく数日ではほぼ動きません(参考値)。"
            "新しいプランでは交差係数→勝率帯→全体係数の順で確率に掛けます(足切りではありません)。"
        ),
    }


@router.get("/calibration-compare")
def calibration_compare(since: Optional[str] = "calibration_switch", db: Session = Depends(get_db)):
    """
    条件別に「補正前(raw)」と「補正後(calibrated)」の予想精度・乖離・p値を並べる。
    rawが無い旧レコードは before 側から除外し、件数を note で明示する。

    2026-09-06修正: 既定でCALIBRATION_SWITCH_AT以降(現行の投票基準)だけに
    絞り込むようにした。全期間を見たい場合は since=all を指定する。
    """
    since_dt = _parse_since_param(since) if since != "all" else None
    pq = (
        db.query(models.Purchase)
        .filter(
            models.Purchase.result != "pending",
            models.Purchase.bet_type == "3連単",
        )
    )
    sq = (
        db.query(models.SkippedBet)
        .filter(
            models.SkippedBet.actual_result.isnot(None),
            models.SkippedBet.bet_type == "3連単",
        )
    )
    if since_dt is not None:
        pq = pq.filter(models.Purchase.purchased_at >= since_dt)
        sq = sq.filter(models.SkippedBet.created_at >= since_dt)
    purchases = pq.all()
    skipped = sq.all()

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
    def prob_bucket_cal(r):
        prob = r.prob_cal if r.prob_cal is not None else r.prob_raw or 0
        name, _ = calc.get_prob_bucket(prob)
        return name

    def bet_type_x_prob_bucket(r):
        return f"{bet_type_bucket(r)} × {prob_bucket_cal(r)}"

    axes = {
        "券種別": bet_type_bucket,
        "勝率帯別": prob_bucket_cal,
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


TARGET_BET_TYPES = ["3連単"]

# 2026-09-01: 補正係数をPurchase/SkippedBetベース(偏りあり)からretroactiveベース
# (偏り無し)へ切り替えた日時。それより前の購入は古い(過剰圧縮された)補正で
# 判断されたものなので、「切り替え後の成果だけを見たい」時の基準点として使う
# (のんの要望により追加: 全期間の集計だと過去の負債が混ざって、今回の修正が
# 効いているかどうか見えにくいため)。
# 集計・診断系エンドポイントが「現行の投票基準」とみなす基準日時。
# 予想ロジック・確率補正・運用ゲートなど投票の中身に関わる修正を入れたら、
# 必ずこの値を修正日の日付に更新すること(更新を忘れると、新旧ロジックの
# 混在データが「現行基準」として集計されてしまい、精度検証の意味が壊れる)。
# 集計・診断系エンドポイントが「現行の投票基準」とみなす基準日時。
# 予想ロジック・確率補正・運用ゲートなど投票の中身に関わる修正を入れたら、
# 必ずこの値を修正日の日付に更新すること(更新を忘れると、新旧ロジックの
# 混在データが「現行基準」として集計されてしまい、精度検証の意味が壊れる)。
#
# 2026-09-08: 実績ゲートを「不調ステージのみ」に修正
# (券種・券種×オッズ帯・購入集合ゲートのプラン混入を除去)。
# JST 2026-09-08 11:00 ≒ UTC 2026-09-08 02:00
#
# Purchase.purchased_at は datetime.utcnow() で保存されるため、naiveな
# 「日付の0時」をJSTの暦日の始まりと取り違えないこと。
# 現行の投票基準へ切り替わった正確な日時。
#
# 集計対象・再投票済み判定・calibration_switch の基準は、
# 暦日ではなく「最後に投票基準を変更したコミット時刻」を使用する。
#
# 最新の投票基準変更コミット:
# 6e29ad4f591bc5ba1f2a647aedf9c0978e5f46fc
# 2026-09-12T01:00:00+09:00
#
# Purchase.purchased_at はUTCのnaive datetimeとして扱われるため、
# 内部比較値はUTCに統一する。
VOTING_CRITERIA_UPDATED_AT = datetime(2026, 9, 11, 16, 0, 0)
# この値は「最後に投票判断そのものを変更した時刻」。
# UI/診断/ログだけの変更では更新しない。
# 再投票済み判定・現行基準集計・calibration_switchはこの値を共通利用する。

# 既存の集計・診断・再投票判定コードとの互換用。
# 今後は VOTING_CRITERIA_UPDATED_AT を「最後の投票基準変更時刻」として扱う。
CALIBRATION_SWITCH_AT = VOTING_CRITERIA_UPDATED_AT
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
    races_skipped_invalid_result = 0
    probability_errors = 0
    calibration_errors = 0
    # 的中組合せが「Oddsに存在しなかった」のか、
    # 「Oddsには存在したが判定できなかった」のかを分離する診断。
    winner_presence = {
        bt: {
            "races": 0,
            "winner_present_in_odds": 0,
            "winner_missing_from_odds": 0,
        }
        for bt in TARGET_BET_TYPES
    }
    winner_missing_races = {bt: [] for bt in TARGET_BET_TYPES}

    # レースごとのN+1クエリを避けるため、OddsをレースID単位で
    # 500レースずつ一括取得する。
    odds_by_race = {}
    race_ids = [race.id for race in races]
    CHUNK = 500

    for i in range(0, len(race_ids), CHUNK):
        chunk = race_ids[i:i + CHUNK]
        odds_rows_chunk = (
            db.query(models.Odds)
            .filter(models.Odds.race_id.in_(chunk))
            .all()
        )
        for odds_row in odds_rows_chunk:
            odds_by_race.setdefault(odds_row.race_id, []).append(odds_row)

    for race in races:
        entries = race.entries
        win_probs = calc.build_win_probs_from_entries(entries)
        if not win_probs:
            races_skipped_no_win_probs += 1
            continue

        odds_rows = odds_by_race.get(race.id) or []
        if not odds_rows:
            races_skipped_no_odds += 1
            continue

        try:
            parsed_result = calc.parse_actual_result(race.actual_result)
        except Exception:
            races_skipped_invalid_result += 1
            continue

        line_map, line_boost = calc.line_map_from_race(race)
        car_numbers_all = sorted(win_probs.keys())
        norm_mass = {}
        for arity in (2, 3):
            if len(car_numbers_all) >= arity:
                mass = calc.total_ordered_mass(
                    win_probs, car_numbers_all, arity, line_map=line_map, line_boost=line_boost,
                )
                norm_mass[arity] = mass if mass > 1e-9 else 1.0
            else:
                norm_mass[arity] = 1.0

        # actual_resultから、その券種で実際に的中する組合せを正規化して作る。
        actual_winners = {}
        canonical = parsed_result.get("canonical_orderings") or []
        top3 = parsed_result.get("top3_set") or set()

        for bt in TARGET_BET_TYPES:
            winners = set()
            if bt == "3連単":
                winners = {"-".join(str(x) for x in order[:3]) for order in canonical if len(order) >= 3}
            elif bt == "2車単":
                winners = {"-".join(str(x) for x in order[:2]) for order in canonical if len(order) >= 2}
            elif bt in ("2車複", "ワイド"):
                if len(top3) >= 2:
                    for order in canonical:
                        if len(order) >= 2:
                            winners.add("-".join(str(x) for x in sorted(order[:2])))
            elif bt == "3連複":
                if len(top3) == 3:
                    winners = {"-".join(str(x) for x in sorted(top3))}

            actual_winners[bt] = winners

            if winners:
                winner_presence[bt]["races"] += 1

        stored_odds_by_type = {}
        for o in odds_rows:
            if o.bet_type in TARGET_BET_TYPES and o.combination:
                combo = str(o.combination)
                if o.bet_type in ("2車複", "ワイド"):
                    try:
                        combo = "-".join(str(x) for x in sorted(int(v) for v in combo.split("-")))
                    except (ValueError, TypeError):
                        pass
                elif o.bet_type == "3連複":
                    try:
                        combo = "-".join(str(x) for x in sorted(int(v) for v in combo.split("-")))
                    except (ValueError, TypeError):
                        pass
                stored_odds_by_type.setdefault(o.bet_type, set()).add(combo)

        for bt in TARGET_BET_TYPES:
            winners = actual_winners.get(bt) or set()
            if not winners:
                continue
            stored = stored_odds_by_type.get(bt) or set()
            if winners & stored:
                winner_presence[bt]["winner_present_in_odds"] += 1
            else:
                winner_presence[bt]["winner_missing_from_odds"] += 1
                winner_missing_races[bt].append({
                    "race_id": race.id,
                    "external_ref": race.external_ref,
                    "venue_name": race.venue_name,
                    "race_date": race.race_date.isoformat() if race.race_date else None,
                    "race_number": race.race_number,
                    "actual_result": race.actual_result,
                    "winner_combinations": sorted(winners),
                })

        races_evaluated += 1

        for o in odds_rows:
            if o.bet_type not in TARGET_BET_TYPES:
                continue
            try:
                cars = tuple(int(x) for x in o.combination.split("-"))
            except (ValueError, AttributeError):
                continue

            try:
                prob_raw = calc.estimate_prob_for_bet(
                    win_probs,
                    o.bet_type,
                    cars,
                    line_map=line_map,
                    line_boost=line_boost,
                )
                arity = calc.BET_TYPE_ARITY.get(o.bet_type)
                if arity in norm_mass:
                    prob_raw = prob_raw / norm_mass[arity]
            except Exception:
                probability_errors += 1
                continue

            try:
                prob_cal = calc.apply_calibration_to_prob(
                    prob_raw,
                    calibration_factors,
                    bet_type=o.bet_type,
                )
            except Exception:
                calibration_errors += 1
                continue

            won = calc.judge_purchase_result(
                o.bet_type,
                o.combination,
                parsed_result,
            )
            key = (race.id, o.bet_type)
            groups_raw_by_type.setdefault(o.bet_type, {}).setdefault(
                key, []
            ).append((o.combination, prob_raw, won))
            groups_cal_by_type.setdefault(o.bet_type, {}).setdefault(
                key, []
            ).append((o.combination, prob_cal, won))
            flat_records_by_type.setdefault(o.bet_type, []).append(
                (prob_raw, prob_cal, won)
            )
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
        "races_skipped_invalid_result": races_skipped_invalid_result,
        "probability_errors": probability_errors,
        "calibration_errors": calibration_errors,
        "winner_presence_by_bet_type": winner_presence,
        "winner_missing_races_by_bet_type": winner_missing_races,
        "by_bet_type": result,
        "message": (
            "これは記録(Purchase/SkippedBet)に一切頼らず、オッズが存在する"
            "組み合わせを毎回全て使って現在のモデルで再計算した結果です。"
            "winner_captured_rate_pctは、この診断で保存済みOddsを評価候補とした場合の"
            "捕捉率です。AIの購入候補生成率ではありません。"
            "winner_presence_by_bet_typeで、実際の的中組合せ自体が保存Oddsに存在したかを"
            "別途確認できます。"
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


@router.get("/calibration-investment-impact")
def calibration_investment_impact(min_ev_pct: float = 5.0, db: Session = Depends(get_db)):
    """
    ChatGPT分析9項目の項目9: 「キャリブレーション改善」と「投資判断改善」の
    指標分離(読み取り専用)。

    `calibration-factors-compare`は予想確率と実績確率の乖離(キャリブレーション
    上の改善)だけを見ており、「乖離が縮んだ=収益が改善した」と早合点しない
    ために、実際に購入した買い目を対象に、券種別の遡及検証係数
    (get_calibration_factors_retroactive の by_bet_type)を適用し直した場合、
    - 実際に買った買い目のうちどれだけが「引き続き買う判定」になるか
    - 除外される側になる買い目は、実際どうだったか(勝率・回収率)
    - 残る側になる買い目は、実際どうだったか
    を分けて集計する。既存の投票ロジック・補正処理は一切変更しない
    (あくまで過去の実購入データへの事後シミュレーション)。
    """
    retro = get_calibration_factors_retroactive(db, use_cache=False)
    retro_by_bet_type = retro.get("by_bet_type") or {}

    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.result.in_(("win", "lose")))
        .all()
    )

    rows = []
    for p in purchases:
        raw_prob = getattr(p, "win_prob_raw", None)
        odds = p.odds_at_purchase or p.final_odds
        if raw_prob is None or not odds:
            continue
        bt_info = retro_by_bet_type.get(p.bet_type)
        if not bt_info:
            continue
        retro_factor = bt_info.get("calibration_factor")
        if retro_factor is None:
            continue
        est_prob_retro = min(1.0, float(raw_prob) * float(retro_factor))
        ev_retro_pct = (est_prob_retro * float(odds) - 1.0) * 100
        rows.append({
            "p": p,
            "would_still_buy": ev_retro_pct >= min_ev_pct,
        })

    def _group_stats(group):
        n = len(group)
        if n == 0:
            return None
        stake = sum(float(r["p"].stake_amount or 0) for r in group)
        payout = sum(float(r["p"].payout_amount or 0) for r in group)
        wins = sum(1 for r in group if r["p"].result == "win")
        return {
            "n": n,
            "win_count": wins,
            "win_rate_pct": round(wins / n * 100, 2),
            "stake_total": round(stake, 0),
            "payout_total": round(payout, 0),
            "roi_pct": round(payout / stake * 100, 2) if stake > 0 else None,
        }

    still_buy = [r for r in rows if r["would_still_buy"]]
    excluded = [r for r in rows if not r["would_still_buy"]]

    return {
        "min_ev_pct": min_ev_pct,
        "evaluated_purchase_count": len(rows),
        "skipped_missing_data_count": len(purchases) - len(rows),
        "would_still_buy_under_retroactive_calibration": _group_stats(still_buy),
        "would_be_excluded_under_retroactive_calibration": _group_stats(excluded),
        "actual_all": _group_stats(rows),
        "note": (
            "実際に買った買い目(win_prob_raw・odds_at_purchaseが記録されているもの"
            "のみ対象)に、券種別の遡及検証係数(偏りの無い方法で計算し直した係数)を"
            "適用し直した場合の事後シミュレーション。"
            "would_be_excluded側の実績回収率が高い場合、遡及検証係数への切り替えで"
            "本来拾えていたはずの利益を取りこぼす可能性があることを示す。逆にこちら側の"
            "実績が悪い(または的中0件)場合は、切り替えても実害は無かった可能性が高い。"
            "『キャリブレーション改善(乖離が縮んだか)』は"
            "/purchases/calibration-factors-compare を参照。こちらは投資結果への"
            "影響だけを見る指標であり、両者は別物として扱うこと。"
            "既存の投票ロジック・補正処理は変更していない。"
        ),
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

    # 2026-09-07追加(ChatGPT分析項目3対応): 「全体一律の補正係数だけでは、
    # 券種・勝率帯ごとの予測誤差の違いを吸収できないのでは」という懸念を判断
    # するための材料。券種単体の係数(by_bet_type)と、その券種×勝率帯の係数
    # (by_bet_type_bucket)がどれだけ乖離しているかを機械的に検出するだけで、
    # 条件別補正を自動導入するものではない(既存の補正処理・投票ロジックは
    # 一切変更していない)。
    condition_specific_correction_candidates = []
    CANDIDATE_FACTOR_GAP_THRESHOLD = 0.15
    for bt, cells in retro_cross.items():
        bt_factor = ((retroactive.get("by_bet_type") or {}).get(bt) or {}).get("calibration_factor")
        if bt_factor is None:
            continue
        for band_name, cell in cells.items():
            cell_factor = cell.get("calibration_factor")
            if cell_factor is None:
                continue
            gap = abs(cell_factor - bt_factor)
            if gap >= CANDIDATE_FACTOR_GAP_THRESHOLD:
                condition_specific_correction_candidates.append({
                    "bet_type": bt,
                    "prob_band": band_name,
                    "bet_type_level_factor": bt_factor,
                    "bet_type_x_band_factor": cell_factor,
                    "gap": round(gap, 3),
                    "sample_count": cell.get("sample_count"),
                    "is_reliable": cell.get("is_reliable"),
                    "significance_p_value_pct": cell.get("significance_p_value_pct"),
                })
    condition_specific_correction_candidates.sort(
        key=lambda r: (not r["is_reliable"], -r["gap"])
    )

    return {
        "overall": overall_compare,
        "by_bucket": by_bucket_compare,
        "by_bet_type": by_bet_type_compare,
        "condition_specific_correction_candidates": condition_specific_correction_candidates,
        "message": (
            "currentは今まで実際の投票判断に使われてきた係数(Purchase/SkippedBet"
            "の記録ベース、偏りの可能性あり)。retroactiveはオッズが存在する組み合わせを"
            "毎回全て使う、偏りの無い方法で計算し直した係数。両者のdeviation_pctや"
            "calibration_factorが大きくずれている場合、現行の係数は偏ったサンプルで"
            "学習されていた可能性が高い。この比較を見て問題なければ、"
            "ev.pyが呼び出す関数をget_calibration_factors_retroactiveに切り替える。"
            "condition_specific_correction_candidatesは、券種単体の補正係数と"
            "券種×勝率帯の補正係数が0.15以上ずれている組み合わせの一覧(is_reliable=True"
            "かつgapが大きいものほど、条件別補正を検討する根拠が強い)。ここに載って"
            "いるからといって自動で条件別補正を導入するわけではなく、判断材料として提示する。"
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
    hours: Optional[float] = None,
    last_n_races: Optional[int] = None,
    race_ids: Optional[list[int]] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    同一の確定済みPurchase集合について予測払戻と実際払戻を比較する。

    絞り込み（replay直後の効果測定用）:
    - hours: 直近N時間（purchased_at 基準）
    - last_n_races: 直近に購入されたレースをN件分
    - since: ISO日時 または calibration_switch（現行投票基準の開始時刻）
    """

    query = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
    )

    since_dt = None
    since_resolved = None
    filter_note = []

    if hours is not None and hours > 0:
        since_dt = datetime.utcnow() - timedelta(hours=float(hours))
        filter_note.append(f"hours={hours}")
    elif since:
        # calibration_switch は現行投票基準の開始時刻ショートカット
        if since == "calibration_switch":
            since_dt = CALIBRATION_SWITCH_AT
        else:
            try:
                since_dt = datetime.fromisoformat(
                    since.replace("Z", "+00:00")
                )
                if since_dt.tzinfo is not None:
                    since_dt = since_dt.replace(tzinfo=None)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="sinceはISO日時、または calibration_switch を指定してください"
                )

    if since_dt is not None:
        if hasattr(models.Purchase, "purchased_at"):
            query = query.filter(models.Purchase.purchased_at >= since_dt)
        elif hasattr(models.Purchase, "created_at"):
            query = query.filter(models.Purchase.created_at >= since_dt)
        since_resolved = since_dt.isoformat()

    if race_ids:
        query = query.filter(models.Purchase.race_id.in_(race_ids))
        filter_note.append(f"race_ids_count={len(race_ids)}")

    purchases = query.all()

    if last_n_races is not None and last_n_races > 0 and purchases:
        filter_note.append(f"last_n_races={last_n_races}")
        race_latest = {}
        for p_obj in purchases:
            ts = getattr(p_obj, "purchased_at", None) or datetime.min
            rid = p_obj.race_id
            if rid not in race_latest or ts > race_latest[rid]:
                race_latest[rid] = ts
        top_races = sorted(
            race_latest.keys(),
            key=lambda r: race_latest[r],
            reverse=True,
        )[: int(last_n_races)]
        top_set = set(top_races)
        purchases = [p_obj for p_obj in purchases if p_obj.race_id in top_set]

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
        if odds < 1000:
            return "300-1000倍"
        if odds < 3000:
            return "1000-3000倍"
        return "3000倍以上"

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
        "300-1000倍",
        "1000-3000倍",
        "3000倍以上",
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

    def prob_band(prob):
        if prob is None:
            return "不明"
        return calc.get_prob_bucket(prob)[0]

    by_prob = {}
    prob_band_order = [name for _, _, name, _ in calc.PROB_BUCKETS] + ["不明"]
    for band in prob_band_order:
        group = [r for r in rows if prob_band(r["prob"]) == band]
        by_prob[band] = finalize(group)

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
        "hours": hours,
        "last_n_races": last_n_races,
        "filter_note": filter_note,
        "race_count": len({r["race_id"] for r in rows}) if rows else 0,
        "overall": overall,
        "by_odds_band": by_odds,
        "by_ev_band": by_ev,
        "by_prob_band": by_prob,
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
            "by_prob_band": (
                "win_prob_at_purchase(補正後確率)の勝率帯別。"
                "prob_odds_predicted_roi_pctとactual_roi_pctの乖離が"
                "どの勝率帯に集中しているかを見る"
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
def profit_concentration(since: Optional[str] = "calibration_switch", db: Session = Depends(get_db)):
    """
    利益がごく一部の大穴的中に偏っていないかを確認する
    (欠落していたエンドポイントをのんの指摘により復旧・再実装)。

    2026-09-06修正: 既定でCALIBRATION_SWITCH_AT以降(現行の投票基準)だけに
    絞り込むようにした。全期間を見たい場合は since=all を指定する。
    """
    since_dt = _parse_since_param(since) if since != "all" else None
    pq = db.query(models.Purchase).filter(models.Purchase.result != "pending")
    if since_dt is not None:
        pq = pq.filter(models.Purchase.purchased_at >= since_dt)
    purchases = pq.all()
    if not purchases:
        return {
            "message": "まだ確定した購入履歴がありません",
            "since": since,
            "since_resolved": since_dt.isoformat() if since_dt else None,
        }

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
            "投資額": round(p.stake_amount, 0),
            "払戻": round(p.payout_amount, 0),
            "利益": round(profit(p), 0),
            "購入時想定勝率%": round(p.win_prob_at_purchase * 100, 2) if p.win_prob_at_purchase is not None else None,
        })

    return {
        "概要": gaiyou,
        "集中度": shuchuudo,
        "判定": hanteil,
        "的中の券種別": bet_result,
        "的中の想定勝率帯別": prob_result,
        "利益の大きい的中_上位": tops,
    }


@router.get("/car-pick-accuracy")
def car_pick_accuracy(since: Optional[str] = "calibration_switch", db: Session = Depends(get_db)):
    """
    券種の組み合わせによるノイズを除き、「そのレースでAIが最有力とした車番」が
    実際に1着/上位3着に来たかどうかだけを追跡する。
    同一レース内の複数買い目が、実質同じ車番予想を券種違いで何度も張っているだけ
    (相関が強く、独立試行として扱えない)という問題を避けた、より純粋な予測精度の指標。
    「1レース=1試行」なので、二項検定もそのまま正しく使える(のんの指摘により追加)。

    2026-09-06修正: このエンドポイントはPurchaseではなくEntry.blended_win_probを
    直接見るため、CALIBRATION_SWITCH_AT以降にPurchase/SkippedBetを持たない
    (=現行ロジックで一度も投票・見送り判定されていない=再投票されていない)
    古いレースは既定で除外するようにした。全期間を見たい場合は since=all。
    """
    since_dt = _parse_since_param(since) if since != "all" else None
    rq = db.query(models.Race).filter(models.Race.actual_result.isnot(None))
    if since_dt is not None:
        already_purchased = (
            db.query(models.Purchase.id)
            .filter(models.Purchase.race_id == models.Race.id)
            .filter(models.Purchase.purchased_at >= since_dt)
        )
        already_skipped = (
            db.query(models.SkippedBet.id)
            .filter(models.SkippedBet.race_id == models.Race.id)
            .filter(models.SkippedBet.created_at >= since_dt)
        )
        rq = rq.filter(or_(already_purchased.exists(), already_skipped.exists()))
    races = rq.options(joinedload(models.Race.entries)).all()
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

    # 2026-09-07修正(ChatGPT分析項目4対応): calibration_significanceと同じ理由で、
    # 実績が予想以上の場合を「偶然のブレの範囲内」と一括りにせず明示する。
    if p_value < 0.05:
        judgement = "予想が実態より高すぎる可能性が高い(偶然では説明しにくい・過大評価の疑い)"
    elif p_value < 0.20:
        judgement = "やや予想が高め(過大評価気味)だが、まだ偶然の範囲とも言える"
    elif win_count / n * 100 >= avg_predicted_pct:
        judgement = "実績的中率が予想平均以上(過大評価の証拠なし。ただしこの検定は実績が予想を上回りすぎていないかは判定しない片側検定)"
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
        "test_direction": "one_sided_lower(実績が予想より低すぎないかだけを検定する片側検定)",
        "judgement": judgement,
        "items": sorted(items, key=lambda x: -x["race_id"]),
    }



@router.get("/diagnostics/prediction-factors")
def prediction_factors_diagnostics(
    since: Optional[str] = "all",
    min_samples: int = 20,
    db: Session = Depends(get_db),
):
    """
    予想精度だけを検証するレース単位診断。

    Purchase/SkippedBet/オッズを評価母集団に使わない。
    これにより「購入フィルターで結果が変わった」のか
    「AIの予想そのものが改善した」のかを分離する。

    主指標:
      - 1着的中率
      - Top3包含率
      - 1-2-3完全順序率

    条件:
      - バンク
      - バンク×予測1着選手×脚質
      - バンク×脚質
      - 同ライン/異なるライン
      - レースステージ
      - グレード
      - 競走得点帯
      - 脚質
    """
    since_dt = _parse_since_param(since) if since != "all" else None

    q = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
    )

    races = q.all()

    def normalize_style(value):
        if not value:
            return "脚質情報なし"
        t = str(value)
        if "逃" in t:
            return "逃げ"
        if "捲" in t:
            return "捲り"
        if "差" in t:
            return "差し"
        if "追" in t:
            return "追込"
        if "両" in t:
            return "両方"
        return t

    def race_event_dt(r):
        return _race_event_dt(r)

    rows = []

    for race in races:
        if since_dt is not None:
            event_dt = race_event_dt(race)
            if event_dt is None or event_dt < since_dt:
                continue

        entries = [e for e in race.entries if e.blended_win_prob is not None]
        if not entries:
            continue

        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue

        groups = parsed.get("groups") or []
        if not groups:
            continue

        actual_order = []
        for g in groups:
            if isinstance(g, (list, tuple, set)):
                actual_order.extend(list(g))
            else:
                actual_order.append(g)

        if len(actual_order) < 3:
            continue

        ranked = sorted(
            entries,
            key=lambda e: float(e.blended_win_prob or 0),
            reverse=True,
        )

        top1 = ranked[0]
        top3 = ranked[:3]

        top1_hit = top1.car_number == actual_order[0]
        top3_hit = top1.car_number in actual_order[:3]

        exact123 = (
            len(top3) >= 3
            and top3[0].car_number == actual_order[0]
            and top3[1].car_number == actual_order[1]
            and top3[2].car_number == actual_order[2]
        )

        second_after_first = (
            len(top3) >= 2
            and top3[0].car_number == actual_order[0]
            and top3[1].car_number == actual_order[1]
        )

        third_after_first_two = (
            len(top3) >= 3
            and top3[0].car_number == actual_order[0]
            and top3[1].car_number == actual_order[1]
            and top3[2].car_number == actual_order[2]
        )

        entry_by_car = {e.car_number: e for e in entries}
        predicted_style = normalize_style(top1.leg_style)
        predicted_player = top1.player_name or f"車番{top1.car_number}"

        line_map = {}
        if race.lines_data:
            for line_idx, line in enumerate(race.lines_data):
                for car in line:
                    try:
                        line_map[int(car)] = line_idx
                    except (TypeError, ValueError):
                        pass

        predicted_line = line_map.get(top1.car_number)
        actual_first_line = line_map.get(actual_order[0])

        if predicted_line is None or actual_first_line is None:
            line_relation = "ライン情報なし"
        elif predicted_line == actual_first_line:
            line_relation = "同ライン"
        else:
            line_relation = "異なるライン"

        score = top1.race_score
        if score is None:
            score_band = "得点情報なし"
        elif score < 55:
            score_band = "55未満"
        elif score < 65:
            score_band = "55-65"
        elif score < 75:
            score_band = "65-75"
        else:
            score_band = "75以上"

        rows.append({
            "race_id": race.id,
            "bank": race.venue_name or "不明",
            "player": predicted_player,
            "style": predicted_style,
            "line_relation": line_relation,
            "stage": race.race_stage or "不明",
            "grade": race.grade or "不明",
            "score_band": score_band,
            "top1_hit": top1_hit,
            "top3_hit": top3_hit,
            "exact123": exact123,
            "second_after_first": second_after_first,
            "third_after_first_two": third_after_first_two,
        })

    if not rows:
        return {
            "message": "条件に該当する予想済み・結果確定レースがありません",
            "since": since,
            "since_resolved": since_dt.isoformat() if since_dt else None,
        }

    def summarize(items):
        n = len(items)
        if not n:
            return None

        top1 = sum(1 for r in items if r["top1_hit"])
        top3 = sum(1 for r in items if r["top3_hit"])
        exact = sum(1 for r in items if r["exact123"])
        second = sum(1 for r in items if r["second_after_first"])
        third = sum(1 for r in items if r["third_after_first_two"])

        return {
            "n": n,
            "top1_hit_count": top1,
            "top1_hit_rate_pct": round(top1 / n * 100, 2),
            "top3_count": top3,
            "top3_rate_pct": round(top3 / n * 100, 2),
            "exact123_count": exact,
            "exact123_rate_pct": round(exact / n * 100, 2),
            "second_after_first_rate_pct": round(second / n * 100, 2),
            "third_after_first_two_rate_pct": round(third / n * 100, 2),
        }

    def grouped(field_names):
        groups = {}
        for r in rows:
            key = " × ".join(str(r[f]) for f in field_names)
            groups.setdefault(key, []).append(r)

        result = []
        for key, items in groups.items():
            if len(items) < min_samples:
                continue
            x = summarize(items)
            x["condition"] = key
            result.append(x)

        result.sort(key=lambda x: (-x["n"], -x["top1_hit_rate_pct"], x["condition"]))
        return result

    overall = summarize(rows)

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "min_samples": min_samples,
        "overall": overall,
        "axes": {
            "バンク": grouped(["bank"]),
            "バンク×予測1着選手×脚質": grouped(["bank", "player", "style"]),
            "バンク×脚質": grouped(["bank", "style"]),
            "予測1着と実際1着のライン関係": grouped(["line_relation"]),
            "レースステージ": grouped(["stage"]),
            "グレード": grouped(["grade"]),
            "予測1着競走得点帯": grouped(["score_band"]),
            "予測1着脚質": grouped(["style"]),
        },
        "note": (
            "この診断は購入履歴・見送り履歴・オッズを評価母集団に使わず、"
            "結果確定済みレースについてAIの予測1着車番を1レース1試行として評価します。"
            "したがって購入フィルターによる見かけ上の改善を予想精度の改善として扱いません。"
            "バンク×予測1着選手×脚質は、バンク条件下で特定選手・脚質の1着予測が"
            "安定しているかを確認するための分析です。"
            "件数がmin_samples未満の条件は表示しません。"
        ),
    }



@router.get("/diagnostics/bank-player-style")
def bank_player_style_diagnostics(
    since: Optional[str] = "all",
    min_samples: int = 10,
    db: Session = Depends(get_db),
):
    """
    バンク×AI予測1着選手×脚質を1レース=1試行で検証する。

    購入履歴を使わず、各レースでAIが最も高く評価した選手を
    「予測1着選手」として評価する。
    これにより購入フィルター・オッズ・投資額による選択バイアスを排除する。
    """
    since_dt = _parse_since_param(since) if since != "all" else None

    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    groups = {}
    overall_n = 0
    overall_wins = 0

    for race in races:
        if since_dt is not None and not _race_is_before_as_of(race, datetime.utcnow()):
            # race_date/post_timeを使った厳密なas-ofではなく、
            # calibration_switch以前/以降の既存データ境界は下で判定する。
            pass

        event_dt = _race_event_dt(race)
        if since_dt is not None:
            if event_dt is None or event_dt < since_dt:
                continue

        candidates = [
            e for e in race.entries
            if e.blended_win_prob is not None
        ]
        if not candidates:
            continue

        top = max(candidates, key=lambda e: e.blended_win_prob)

        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue

        groups_actual = parsed.get("groups") or []
        if not groups_actual:
            continue

        actual_first = groups_actual[0]
        won = top.car_number in actual_first

        style = str(top.leg_style or "脚質情報なし")
        style = style.replace("追", "追込") if style == "追" else style
        bank = race.venue_name or "バンク情報なし"
        player = top.player_name or "選手名不明"

        key = (bank, player, style)

        g = groups.setdefault(
            key,
            {
                "bank": bank,
                "player": player,
                "leg_style": style,
                "n": 0,
                "wins": 0,
            },
        )

        g["n"] += 1
        g["wins"] += int(won)

        overall_n += 1
        overall_wins += int(won)

    if overall_n == 0:
        return {
            "message": "条件に該当するAI予測済みレースがありません",
            "since": since,
        }

    overall_rate = overall_wins / overall_n

    rows = []
    for g in groups.values():
        if g["n"] < min_samples:
            continue

        rate = g["wins"] / g["n"]
        lo, hi = calc.wilson_score_interval(g["wins"], g["n"])

        rows.append({
            "bank": g["bank"],
            "player": g["player"],
            "leg_style": g["leg_style"],
            "n": g["n"],
            "wins": g["wins"],
            "hit_rate_pct": round(rate * 100, 2),
            "ci95_low_pct": round(lo * 100, 2),
            "ci95_high_pct": round(hi * 100, 2),
            "delta_vs_overall_pt": round((rate - overall_rate) * 100, 2),
        })

    rows.sort(
        key=lambda r: (-r["n"], -r["hit_rate_pct"], r["bank"], r["player"])
    )

    return {
        "since": since,
        "min_samples": min_samples,
        "overall": {
            "n": overall_n,
            "wins": overall_wins,
            "hit_rate_pct": round(overall_rate * 100, 2),
        },
        "groups": rows,
        "group_count": len(rows),
        "note": (
            "購入履歴ではなく、各レースのAI予測1位車番を1試行として評価しています。"
            "選手はAIが1着予想した車番の選手、脚質はその選手の脚質です。"
            "オッズ・購入フィルター・投資額はこの診断の評価には使用しません。"
        ),
    }




@router.get("/diagnostics/line-position-matrix")
def line_position_matrix_diagnostics(
    since: Optional[str] = "all",
    min_samples: int = 20,
    db: Session = Depends(get_db),
):
    """
    ライン内位置ペア別の予測と実績を検証する。

    目的:
      「先頭→番手」「番手→3番手」など、
      ライン内の位置関係そのものに予測上の優位性があるかを確認する。

    購入履歴・SkippedBet・オッズは使用しない。
    結果確定済みRace/Entryだけを使用する。

    予測確率:
      harville_prob((A,B))
      = 現在の1着確率モデルから算出した
        Aが1着、Bが2着になる条件付き確率。

    実績:
      実際の1着→2着、2着→3着の隣接遷移。

    経験的補正倍率:
      実際の遷移回数 / 無補正予測確率合計

    これは投票条件を直接変更する診断ではなく、
    位置関係に系統的なズレがあるかを確認するための読み取り専用分析。
    """

    since_dt = _parse_since_param(since) if since != "all" else None

    races = (
        db.query(models.Race)
        .filter(models.Race.actual_result.isnot(None))
        .options(joinedload(models.Race.entries))
        .all()
    )

    # 区分ごとの集計
    stats = {}

    evaluated_races = 0
    evaluated_transitions = 0

    def normalize_position(pos):
        if pos is None:
            return None
        if pos == 0:
            return "先頭"
        if pos == 1:
            return "番手"
        return "3番手以降"

    def add_stat(category, predicted_prob, actual):
        g = stats.setdefault(
            category,
            {
                "候補として現れた延べ回数": 0,
                "無補正モデルの予測確率合計": 0.0,
                "実際にその遷移が起きた回数": 0,
            },
        )
        g["候補として現れた延べ回数"] += 1
        g["無補正モデルの予測確率合計"] += float(predicted_prob or 0.0)
        g["実際にその遷移が起きた回数"] += int(actual)

    for race in races:
        event_dt = _race_event_dt(race)

        if since_dt is not None:
            if event_dt is None or event_dt < since_dt:
                continue

        entries = [
            e for e in race.entries
            if e.blended_win_prob is not None
        ]

        if len(entries) < 3:
            continue

        try:
            parsed = calc.parse_actual_result(race.actual_result)
        except Exception:
            continue

        canonical = parsed.get("canonical_orderings") or []
        if not canonical:
            continue

        # 同着を含む場合は、最初のcanonical orderを使用する。
        # 位置ペア診断では同着による重複カウントを避ける。
        actual_order = list(canonical[0])
        if len(actual_order) < 3:
            continue

        line_map, line_boost = calc.line_map_from_race(race)
        pos_map = calc.line_position_map(race)

        if not line_map or not pos_map:
            continue

        win_probs = {
            int(e.car_number): float(e.blended_win_prob)
            for e in entries
            if e.car_number is not None
        }

        if len(win_probs) < 3:
            continue

        cars = sorted(win_probs.keys())

        # --------------------------------------------------------
        # 全候補の「A→B」遷移をモデル確率で集計
        # --------------------------------------------------------
        for a in cars:
            for b in cars:
                if a == b:
                    continue

                pos_a = pos_map.get(a)
                pos_b = pos_map.get(b)

                if pos_a is None or pos_b is None:
                    continue

                same_line = (
                    line_map.get(a) is not None
                    and line_map.get(a) == line_map.get(b)
                )

                relation = "同ライン" if same_line else "異ライン"

                pos_a_label = normalize_position(pos_a)
                pos_b_label = normalize_position(pos_b)

                category = f"{relation}: {pos_a_label}→{pos_b_label}"

                try:
                    predicted_prob = calc.harville_prob(
                        win_probs,
                        (a, b),
                        line_map=line_map,
                        line_boost=line_boost,
                    )
                except Exception:
                    continue

                # 実績側はこの後の1→2 / 2→3と一致する場合のみ1
                actual = 0

                # 1→2
                if (
                    len(actual_order) >= 2
                    and actual_order[0] == a
                    and actual_order[1] == b
                ):
                    actual = 1

                # 2→3
                elif (
                    len(actual_order) >= 3
                    and actual_order[1] == a
                    and actual_order[2] == b
                ):
                    actual = 1

                add_stat(category, predicted_prob, actual)

        evaluated_races += 1
        evaluated_transitions += 2

    if not stats:
        return {
            "message": "位置ペアとして分析可能なレースがありません",
            "since": since,
            "since_resolved": since_dt.isoformat() if since_dt else None,
        }

    rows = []

    for category, g in stats.items():
        n = g["候補として現れた延べ回数"]
        predicted = g["無補正モデルの予測確率合計"]
        actual = g["実際にその遷移が起きた回数"]

        if n < min_samples:
            continue

        multiplier = None
        if predicted > 1e-12:
            multiplier = actual / predicted

        rows.append({
            "区分": category,
            "候補として現れた延べ回数": n,
            "無補正モデルの予測確率合計": round(predicted, 6),
            "実際にその遷移が起きた回数": actual,
            "経験的補正倍率の目安(実際÷予測)": (
                round(multiplier, 4)
                if multiplier is not None
                else None
            ),
        })

    rows.sort(
        key=lambda x: (
            -x["候補として現れた延べ回数"],
            x["区分"],
        )
    )

    return {
        "since": since,
        "since_resolved": since_dt.isoformat() if since_dt else None,
        "min_samples": min_samples,
        "評価対象レース数": evaluated_races,
        "評価対象遷移数(1着→2着+2着→3着)": evaluated_transitions,
        "位置ペア区分別": rows,
        "note": (
            "購入履歴・SkippedBet・オッズを使わず、結果確定済みレースの"
            "ライン内位置関係を分析しています。"
            "予測値は現在のHarville型1着確率モデルから算出したA→Bの"
            "2着までの遷移確率です。"
            "実績値は各レースの1→2および2→3の実際の遷移です。"
            "経験的補正倍率は実際の遷移回数÷モデル予測確率合計です。"
            "1.0より小さい場合はモデルがその位置ペアを過大評価、"
            "1.0より大きい場合は過小評価していることを意味します。"
            "この値だけでは十分条件ではないため、母数と他の位置ペアとの比較を"
            "併せて判断します。"
        ),
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


_purchase_stats_cache = {"computed_at": 0.0, "value": None}
PURCHASE_STATS_CACHE_TTL_SECONDS = 60 * 10  # 10分。全件スキャンで数十秒かかるため、連打で毎回再計算しない


@router.get("/stats")
def purchase_stats(refresh: bool = False, since: Optional[str] = "calibration_switch", db: Session = Depends(get_db)):
    """
    勝率帯別・券種別の回収率など、複数の切り口で集計する。
    単一要素だけで結論づけないためのFX版ルールを踏襲。

    2026-09-06修正: 既定でCALIBRATION_SWITCH_AT(現行の投票基準の開始日時)以降の
    データだけに絞り込むようにした。以前はsinceの絞り込みが無く、投票ロジックが
    変わる前の旧データまで「現行基準の集計」として表示されていた(のんの指摘で発覚)。
    全期間を見たい場合は since=all を指定する。

    2026-09-06追加: 購入・見送り全件+関連オッズを毎回スキャンするため重く
    (実測80秒超)、Neonの転送量も無視できないので既定条件(since=calibration_switch)
    の結果だけ10分キャッシュする。最新のreplay結果をすぐ見たい時は refresh=true。
    """
    import time as _time

    since = since or "calibration_switch"
    use_cache = not refresh and since == "calibration_switch"

    if use_cache:
        cached = _purchase_stats_cache["value"]
        if cached is not None and (_time.time() - _purchase_stats_cache["computed_at"]) < PURCHASE_STATS_CACHE_TTL_SECONDS:
            return {**cached, "cache_hit": True, "cached_at": _purchase_stats_cache["computed_at"]}

    since_dt = _parse_since_param(since) if since != "all" else None
    result = _compute_purchase_stats(db, since_dt)
    if "message" not in result:
        result["since"] = since
        result["since_resolved"] = since_dt.isoformat() if since_dt else None
        if use_cache:
            _purchase_stats_cache["value"] = result
            _purchase_stats_cache["computed_at"] = _time.time()
    return {**result, "cache_hit": False}


def _compute_purchase_stats(db: Session, since_dt=None):
    pq = db.query(models.Purchase).filter(models.Purchase.result != "pending")
    if since_dt is not None:
        pq = pq.filter(models.Purchase.purchased_at >= since_dt)
    purchases_only = pq.all()
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
    sq = db.query(models.SkippedBet).filter(models.SkippedBet.actual_result.isnot(None))
    if since_dt is not None:
        sq = sq.filter(models.SkippedBet.created_at >= since_dt)
    skipped_eval = sq.all()
    purchases = list(purchases_only) + [_SkippedAsPurchase(s) for s in skipped_eval]
    if not purchases:
        pending_n = db.query(models.Purchase).filter(models.Purchase.result == "pending").count()
        all_settled_n = db.query(models.Purchase).filter(models.Purchase.result != "pending").count()
        return {
            "message": "まだ確定した購入履歴がありません",
            "hint": (
                "集計期間(since)以降に result!=pending の購入が0件です。"
                "JSTとUTCのずれ、または since=calibration_switch の境界を確認してください。"
                "全期間は /purchases/stats?since=all"
            ),
            "pending_count": pending_n,
            "settled_count_all_time": all_settled_n,
            "since_filter_active": since_dt is not None,
            "since_resolved": since_dt.isoformat() if since_dt else None,
        }

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
    # 2026-09-06修正: 以前はレースごとの3連単オッズを全組み合わせ分(1レース最大500件超)
    # まるごと読み込んでPython側で最小値を探していたため、対象レースが増えるほど
    # 転送量・処理時間が線形以上に膨らんでいた(実測86秒でタイムアウトの原因)。
    # SQL側でGROUP BY MINするだけで済むので、1レース1行だけ転送する形に変更。
    top_fav_odds_by_race = {}
    if race_ids:
        min_odds_rows = (
            db.query(models.Odds.race_id, func.min(models.Odds.odds_value))
            .filter(models.Odds.race_id.in_(race_ids), models.Odds.bet_type == "3連単")
            .group_by(models.Odds.race_id)
            .all()
        )
        top_fav_odds_by_race = {race_id: odds_value for race_id, odds_value in min_odds_rows}

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
        # 2026-09-07 指標を再定義(のんの指摘により変更):
        #   予想 = 見送りも含む評価対象全件のAI見積り(補正後)。従来の「想定」の計算式を流用。
        #   想定 = 実際に投票プランで投票した対象「だけ」のAI見積り(補正後)。
        #   実績 = 実際に投票プランで投票した対象「だけ」の実際の結果(的中率・回収率)。
        # 想定と実績の集計母集団を揃えることで、両者を直接比較できるようにする。
        buckets = {}
        for p in purchases:
            key = key_fn(p)
            b = buckets.setdefault(key, {
                "stake": 0.0, "payout": 0.0, "count": 0,
                "win_prob_sum": 0.0, "win_prob_count": 0,
                "ev_pct_sum": 0.0, "ev_pct_count": 0,
                "purchased_count": 0, "purchased_wins": 0,
                "purchased_win_prob_sum": 0.0, "purchased_win_prob_count": 0,
                "purchased_ev_pct_sum": 0.0, "purchased_ev_pct_count": 0,
            })
            b["count"] += 1
            # 予想: win_prob_at_purchase / ev_pct_at_purchase(AI見積り・補正後)を
            # 実際に投票したかどうかに関係なく全件単純平均する。
            if p.win_prob_at_purchase is not None:
                b["win_prob_sum"] += p.win_prob_at_purchase
                b["win_prob_count"] += 1
            if p.ev_pct_at_purchase is not None:
                b["ev_pct_sum"] += p.ev_pct_at_purchase
                b["ev_pct_count"] += 1
            # 見送り(SkippedBet)は実際にお金を賭けていない(stake=0固定)ため、
            # 想定・実績はどちらも実際に投票した対象だけを対象にする。
            if p.stake_amount > 0:
                b["purchased_count"] += 1
                b["stake"] += p.stake_amount
                b["payout"] += p.payout_amount
                if p.result == "win":
                    b["purchased_wins"] += 1
                if p.win_prob_at_purchase is not None:
                    b["purchased_win_prob_sum"] += p.win_prob_at_purchase
                    b["purchased_win_prob_count"] += 1
                if p.ev_pct_at_purchase is not None:
                    b["purchased_ev_pct_sum"] += p.ev_pct_at_purchase
                    b["purchased_ev_pct_count"] += 1
        out = {}
        for k, v in buckets.items():
            has_purchase = v["purchased_count"] > 0
            expectancy = ((v["payout"] - v["stake"]) / v["stake"] * 100) if has_purchase else None
            # 予想的中率/予想回収率: 見送りも含む評価対象全件の単純平均(旧「想定」と同じ計算式)。
            predicted_win_rate_pct = (
                round(v["win_prob_sum"] / v["win_prob_count"] * 100, 1) if v["win_prob_count"] else None
            )
            predicted_roi_pct = (
                round(v["ev_pct_sum"] / v["ev_pct_count"] + 100, 2) if v["ev_pct_count"] else None
            )
            # 想定的中率/想定回収率: 実際に投票した対象だけの単純平均(実績と同じ母集団)。
            expected_win_rate_pct = (
                round(v["purchased_win_prob_sum"] / v["purchased_win_prob_count"] * 100, 1)
                if v["purchased_win_prob_count"] else None
            )
            expected_roi_pct = (
                round(v["purchased_ev_pct_sum"] / v["purchased_ev_pct_count"] + 100, 2)
                if v["purchased_ev_pct_count"] else None
            )
            # 実的中率: 実際に投票した対象だけの実際の的中率(想定と同じ母集団)。
            win_rate_pct = (
                round(v["purchased_wins"] / v["purchased_count"] * 100, 1) if has_purchase else None
            )
            # 2026-09-07追加(ChatGPT分析項目5対応): 「回収率が高い」というだけで
            # 小サンプルの条件を有効と判断しないよう、実的中率の95%信頼区間
            # (Wilson score interval)を併記する。件数が少ないほど区間が広くなり、
            # 一目で「まだ何とも言えない」ことが分かるようにする。
            win_rate_ci = None
            if has_purchase:
                lo, hi = calc.wilson_score_interval(v["purchased_wins"], v["purchased_count"])
                win_rate_ci = {"ci95_low_pct": round(lo * 100, 1), "ci95_high_pct": round(hi * 100, 1)}
            out[k] = {
                "count": v["count"],
                "purchased_count": v["purchased_count"],
                "win_rate_pct": win_rate_pct,
                "win_rate_ci95": win_rate_ci,
                "predicted_win_rate_pct": predicted_win_rate_pct,
                "expected_win_rate_pct": expected_win_rate_pct,
                # roi_pct: 回収率(100%が損益分岐点)。expectancy_pct: 同じ値を「0%が損益分岐点」の表現にしたもの。
                # 実際に購入した件数が0件(見送りのみ)の場合はNone(集計不可)にする。
                "roi_pct": round(expectancy + 100, 2) if expectancy is not None else None,
                "expectancy_pct": round(expectancy, 2) if expectancy is not None else None,
                "profit": round(v["payout"] - v["stake"], 0) if has_purchase else None,
                "predicted_roi_pct": predicted_roi_pct,
                "expected_roi_pct": expected_roi_pct,
                "expected_profit": None,
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

        "季節×バンク先行有利度": combo_bucket(season_bucket, "季節", bank_lead_bucket, "先行有利度"),
        "券種×ライン絡み": combo_bucket(lambda p: p.bet_type, "券種", line_bucket, "ライン"),

        # 2026-09-07追加(ChatGPT分析項目7対応): 同ライン絡みの高回収率が
        # 「同ラインだから」なのか「同ライン条件に高オッズの買い目が集中して
        # いるだけ」なのかを切り分けるための組み合わせ。

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
                    "predicted_win_rate_pct": v["predicted_win_rate_pct"],
                    "expected_win_rate_pct": v["expected_win_rate_pct"],
                    "expectancy_pct": v["expectancy_pct"],
                    "predicted_roi_pct": v["predicted_roi_pct"],
                    "expected_roi_pct": v["expected_roi_pct"],
                })
    # 見送りのみ(実績算出不可=expectancy_pct が None)の条件はランキングに含めない
    # (のんの指摘により修正。以前は0%扱いされ、大穴帯の見送りばかりが
    # 「不調な条件」の上位を占めてしまっていた)。
    ranking = [r for r in ranking if r["expectancy_pct"] is not None]
    ranking.sort(key=lambda x: -x["expectancy_pct"])

    # 2026-09-07 指標を再定義(のんの指摘により変更):
    #   予想 = 見送りも含む評価対象全件のAI見積り(補正後)。従来の「想定」の計算式をそのまま流用。
    #   想定 = 実際に投票プランで投票した対象「だけ」のAI見積り(補正後)。
    #   実的中率 = 実際に投票プランで投票した対象「だけ」の実際の的中率。
    # 想定と実的中率・実績収支率(overall_roi_pct)の集計母集団を揃えることで、
    # 「想定通りの結果になっているか」を直接比較できるようにする。
    #
    # 予想(旧・想定の計算式): ev_pct_at_purchaseは「0%が損益分岐点」の表現(calc_ev_pctの定義)。
    # 実績収支率(100%が損益分岐点)と単位を揃えるため+100する。レースごとに投資額が
    # 異なるため、実績収支率(payout/stake)と同じ金額加重で計算する(のんの指摘により修正)。
    win_prob_values = [p.win_prob_at_purchase for p in purchases if p.win_prob_at_purchase is not None]
    ev_purchases = [p for p in purchases if p.ev_pct_at_purchase is not None]
    predicted_win_rate_pct = round(sum(win_prob_values) / len(win_prob_values) * 100, 1) if win_prob_values else None
    predicted_stake_sum = sum(p.stake_amount for p in ev_purchases)
    predicted_profit_sum = sum(p.stake_amount * p.ev_pct_at_purchase / 100 for p in ev_purchases)
    predicted_roi_pct = (
        round((predicted_profit_sum / predicted_stake_sum + 1) * 100, 2) if predicted_stake_sum else None
    )
    predicted_profit_total = round(predicted_profit_sum, 0) if ev_purchases else None

    # 想定: 実際に投票した対象だけに絞り込んだ上で、同じ計算式(金額加重)を適用する。
    purchased_only_for_stats = [p for p in purchases if p.stake_amount > 0]
    purchased_win_prob_values = [
        p.win_prob_at_purchase for p in purchased_only_for_stats if p.win_prob_at_purchase is not None
    ]
    purchased_ev_purchases = [p for p in purchased_only_for_stats if p.ev_pct_at_purchase is not None]
    expected_win_rate_pct = (
        round(sum(purchased_win_prob_values) / len(purchased_win_prob_values) * 100, 1)
        if purchased_win_prob_values else None
    )
    expected_stake_sum = sum(p.stake_amount for p in purchased_ev_purchases)
    expected_profit_sum = sum(p.stake_amount * p.ev_pct_at_purchase / 100 for p in purchased_ev_purchases)
    expected_roi_pct = (
        round((expected_profit_sum / expected_stake_sum + 1) * 100, 2) if expected_stake_sum else None
    )
    expected_profit_total = round(expected_profit_sum, 0) if purchased_ev_purchases else None

    # 実的中率: 実際に投票した対象だけの実際の的中率(想定と同じ母集団)。
    overall_win_count = sum(1 for p in purchased_only_for_stats if p.result == "win")
    overall_win_rate_pct = (
        round(overall_win_count / len(purchased_only_for_stats) * 100, 1) if purchased_only_for_stats else None
    )

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
        # 修正(2026-09-08・のんの指摘): purchasesは実購入(Purchase)と
        # 見送り記録(SkippedBet)を合体させたリストで、見送り分は投資額・払戻額
        # ともに常に0円として登録される。そのため実購入が1件も無く見送りだけの
        # レースまで「払戻(0円)>=投資額(0円)」でTrueとなり、誤って黒字レースに
        # 数えられてしまっていた(245レース中29黒字という数字は、実購入があった
        # 224レース中の本当の黒字8件に、見送りのみの21レースが紛れ込んだ結果で
        # あることを実測で確認済み)。実際に購入があったレースだけを対象にする。
        real_purchase_records = [
            p for p in purchases if not getattr(p, "is_skipped_record", False)
        ]
        race_ids_with_purchase = sorted({p.race_id for p in real_purchase_records})
        race_level_results = []
        for rid in race_ids_with_purchase:
            race_purchases = [p for p in real_purchase_records if p.race_id == rid]
            race_stake = sum(p.stake_amount for p in race_purchases)
            race_payout = sum(p.payout_amount for p in race_purchases)
            race_level_results.append(1 if race_payout >= race_stake else 0)
        n_races = len(race_level_results)
        profit_races = sum(race_level_results)

        # 2026-09-07修正(ChatGPT分析項目4対応): この検定は「実績が予想より
        # 低すぎないか」だけを見る片側検定のため、実績が予想と同等かそれ以上
        # だと数式上p値は自動的に100%近くになる(過大評価の証拠が無い、という
        # 意味であり、「大成功」を示す値ではない)。この性質を判定文言で
        # 明示しないと、p値100%が何を意味するのか誤解される
        # (例: 3連単×勝率0-5%、勝率5-15%帯など、実績的中率が予想平均を
        # 上回っていた条件でp値100%表示が出ていたのは、計算バグではなく
        # この片側検定の仕様通りの挙動だったことを確認済み)。
        deviation_pct_bp = round((wins_with_prob / n_with_prob - avg_predicted_prob) * 100, 3) if n_with_prob else 0.0
        if p_value < 0.05:
            judgement = "予想が実態より高すぎる可能性が高い(偶然では説明しにくい・過大評価の疑い)"
        elif p_value < 0.20:
            judgement = "やや予想が高め(過大評価気味)だが、まだ偶然の範囲とも言える"
        elif deviation_pct_bp > 0:
            judgement = "実績的中率が予想平均以上(過大評価の証拠なし。ただしこの検定は実績が予想を上回りすぎていないかは判定しない片側検定)"
        else:
            judgement = "現時点のサンプル数では、偶然のブレの範囲内"
        calibration_significance = {
            "p_value_pct": round(p_value * 100, 4),
            "judgement": judgement,
            "test_direction": "one_sided_lower(実績が予想より低すぎないかだけを検定する片側検定)",
            "deviation_pct": deviation_pct_bp,
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
        "predicted_roi_pct": predicted_roi_pct,
        "predicted_profit_total": predicted_profit_total,
        "expected_roi_pct": expected_roi_pct,
        "expected_profit_total": expected_profit_total,
        "overall_win_rate_pct": overall_win_rate_pct,
        "predicted_win_rate_pct": predicted_win_rate_pct,
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



@router.get("/diagnostics/purchase-selection-calibration")
def purchase_selection_calibration(
    since: Optional[str] = None,
    race_ids: Optional[list[int]] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    実際に購入した買い目だけを対象に、
    確率帯・オッズ帯・EV帯ごとの校正誤差を確認する。

    全体の確率格子が正しくても、EV上位抽出後の購入群だけで
    確率が過大評価されている可能性を検証する。
    """

    since_dt = _parse_since_param(since)

    q = (
        db.query(models.Purchase)
        .filter(models.Purchase.result != "pending")
        .filter(models.Purchase.stake_amount > 0)
        .filter(models.Purchase.win_prob_at_purchase.isnot(None))
    )

    if since_dt:
        q = q.filter(models.Purchase.purchased_at >= since_dt)

    if race_ids:
        q = q.filter(models.Purchase.race_id.in_(race_ids))

    purchases = q.all()

    def prob_band(p):
        name, _ = calc.get_prob_bucket(float(p.win_prob_at_purchase))
        return name

    def odds_band(p):
        o = p.odds_at_purchase
        if o is None or o <= 0:
            return "不明"
        o = float(o)
        if o < 5:
            return "1-5倍"
        if o < 10:
            return "5-10倍"
        if o < 30:
            return "10-30倍"
        if o < 100:
            return "30-100倍"
        if o < 300:
            return "100-300倍"
        return "300倍以上"

    def ev_band(p):
        ev = p.ev_pct_at_purchase
        if ev is None:
            return "不明"
        ev = float(ev)
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

    def stats(rows):
        if not rows:
            return {
                "n": 0,
                "wins": 0,
                "predicted_avg_pct": None,
                "actual_hit_rate_pct": None,
                "gap_pt": None,
                "expected_hits": 0.0,
                "actual_hits": 0,
                "hit_gap": None,
                "avg_odds": None,
                "avg_ev_pct": None,
                "predicted_roi_pct": None,
                "actual_roi_pct": None,
                "p_value_lower_tail_pct": None,
            }

        probs = [float(p.win_prob_at_purchase) for p in rows]
        wins = sum(1 for p in rows if p.result == "win")
        n = len(rows)

        pred_avg = sum(probs) / n
        actual = wins / n

        odds_rows = [
            p for p in rows
            if p.odds_at_purchase is not None and p.odds_at_purchase > 0
        ]

        avg_odds = (
            sum(float(p.odds_at_purchase) for p in odds_rows) / len(odds_rows)
            if odds_rows else None
        )

        ev_rows = [
            p for p in rows
            if p.ev_pct_at_purchase is not None
        ]

        avg_ev = (
            sum(float(p.ev_pct_at_purchase) for p in ev_rows) / len(ev_rows)
            if ev_rows else None
        )

        stake = sum(float(p.stake_amount or 0) for p in rows)

        predicted_return = sum(
            float(p.stake_amount or 0)
            * float(p.win_prob_at_purchase)
            * float(p.odds_at_purchase)
            for p in odds_rows
        )

        actual_return = sum(
            float(p.payout_amount or 0)
            for p in rows
        )

        p_value = calc.binomial_lower_tail_p(
            wins,
            n,
            pred_avg,
        )

        return {
            "n": n,
            "wins": wins,
            "predicted_avg_pct": round(pred_avg * 100, 4),
            "actual_hit_rate_pct": round(actual * 100, 4),
            "gap_pt": round((actual - pred_avg) * 100, 4),
            "expected_hits": round(sum(probs), 4),
            "actual_hits": wins,
            "hit_gap": round(wins - sum(probs), 4),
            "avg_odds": round(avg_odds, 4) if avg_odds is not None else None,
            "avg_ev_pct": round(avg_ev, 4) if avg_ev is not None else None,
            "predicted_roi_pct": (
                round(predicted_return / stake * 100, 4)
                if stake > 0 and odds_rows else None
            ),
            "actual_roi_pct": (
                round(actual_return / stake * 100, 4)
                if stake > 0 else None
            ),
            "p_value_lower_tail_pct": round(p_value * 100, 8),
        }

    prob_order = [
        "0-5%(大穴)",
        "5-15%",
        "15-30%",
        "30%以上(本命)",
    ]

    odds_order = [
        "1-5倍",
        "5-10倍",
        "10-30倍",
        "30-100倍",
        "100-300倍",
        "300倍以上",
        "不明",
    ]

    ev_order = [
        "EVマイナス",
        "0-20%",
        "20-50%",
        "50-100%",
        "100-300%",
        "300%以上",
        "不明",
    ]

    by_prob = {
        band: stats([p for p in purchases if prob_band(p) == band])
        for band in prob_order
    }

    by_prob_odds = {}
    for pb in prob_order:
        for ob in odds_order:
            rows = [
                p for p in purchases
                if prob_band(p) == pb and odds_band(p) == ob
            ]
            if rows:
                by_prob_odds[f"{pb} × {ob}"] = stats(rows)

    by_ev_odds = {}
    for eb in ev_order:
        for ob in odds_order:
            rows = [
                p for p in purchases
                if ev_band(p) == eb and odds_band(p) == ob
            ]
            if rows:
                by_ev_odds[f"{eb} × {ob}"] = stats(rows)

    # レース単位診断
    #
    # 同一レース内の複数買い目は独立ではないため、
    # 「各買い目の確率を合計した期待的中数」と
    # 「実際に1つ以上的中したか」を別途確認する。
    by_race = {}
    for p in purchases:
        by_race.setdefault(p.race_id, []).append(p)

    race_rows = []

    for race_id, rows in by_race.items():
        prob_sum = sum(
            float(p.win_prob_at_purchase)
            for p in rows
        )

        hit = any(p.result == "win" for p in rows)

        # 同一レース内の買い目確率合計が1を超える場合、
        # 「少なくとも1つ当たる確率」とは一致しないため、
        # 合計値は参考情報として別表示する。
        race_rows.append({
            "race_id": race_id,
            "prob_sum": prob_sum,
            "hit": hit,
            "bet_count": len(rows),
        })

    race_count = len(race_rows)
    race_hits = sum(1 for r in race_rows if r["hit"])
    race_prob_sum_total = sum(r["prob_sum"] for r in race_rows)

    # 確率和は「期待的中買い目数」であり、
    # race_hit_rateの直接予測値ではないことを明示する。
    race_summary = {
        "race_count": race_count,
        "races_with_at_least_one_hit": race_hits,
        "race_hit_rate_pct": (
            round(race_hits / race_count * 100, 4)
            if race_count else None
        ),
        "sum_of_purchase_probabilities": round(race_prob_sum_total, 4),
        "actual_hit_race_count": race_hits,
        "note": (
            "同一レース内の買い目は相関するため、確率合計は"
            "『期待的中買い目数』であり、"
            "『1つ以上当たるレース確率』と直接比較していません。"
        ),
    }

    overall = stats(purchases)

    # 購入時オッズと確定時オッズの乖離が、
    # PVAの予測払戻と実払戻の差を説明できるか確認する。
    drift_rows = [
        p for p in purchases
        if p.odds_at_purchase is not None
        and float(p.odds_at_purchase) > 0
        and p.final_odds is not None
        and float(p.final_odds) > 0
    ]

    drift_wins = [
        p for p in drift_rows
        if p.result == "win"
    ]

    def drift_detail(rows):
        if not rows:
            return {
                "n": 0,
                "wins": 0,
                "avg_purchase_odds": None,
                "avg_final_odds": None,
                "avg_drift_pct": None,
                "worsened_ratio_pct": None,
                "expected_payout_at_purchase_odds": 0.0,
                "actual_payout_at_final_odds": 0.0,
                "actual_vs_expected_payout_pct": None,
            }

        avg_purchase = sum(
            float(p.odds_at_purchase) for p in rows
        ) / len(rows)

        avg_final = sum(
            float(p.final_odds) for p in rows
        ) / len(rows)

        drifts = [
            (
                float(p.final_odds) - float(p.odds_at_purchase)
            ) / float(p.odds_at_purchase) * 100
            for p in rows
        ]

        wins = [p for p in rows if p.result == "win"]

        expected_payout = sum(
            float(p.stake_amount or 0)
            * float(p.odds_at_purchase)
            for p in wins
        )

        actual_payout = sum(
            float(p.payout_amount or 0)
            for p in wins
        )

        return {
            "n": len(rows),
            "wins": len(wins),
            "avg_purchase_odds": round(avg_purchase, 4),
            "avg_final_odds": round(avg_final, 4),
            "avg_drift_pct": round(sum(drifts) / len(drifts), 4),
            "worsened_ratio_pct": round(
                sum(1 for d in drifts if d < 0) / len(drifts) * 100,
                4,
            ),
            "expected_payout_at_purchase_odds": round(expected_payout, 2),
            "actual_payout_at_final_odds": round(actual_payout, 2),
            "actual_vs_expected_payout_pct": (
                round(actual_payout / expected_payout * 100, 4)
                if expected_payout > 0 else None
            ),
        }

    def drift_odds_band(p):
        o = float(p.odds_at_purchase)
        if o < 100:
            return "30-100倍"
        if o < 300:
            return "100-300倍"
        return "300倍以上"

    drift_by_odds = {
        band: drift_detail([
            p for p in drift_wins
            if drift_odds_band(p) == band
        ])
        for band in ("30-100倍", "100-300倍", "300倍以上")
    }

    odds_drift_detail = {
        "all_settled_with_final_odds": drift_detail(drift_rows),
        "wins_only": drift_detail(drift_wins),
        "wins_by_purchase_odds": drift_by_odds,
        "note": (
            "wins_onlyのactual_vs_expected_payout_pctが100%未満なら、"
            "購入時オッズで予測した的中払戻より確定時払戻が低下している。"
            "PVAのROI乖離の一因としてオッズ変動を直接確認するための診断値。"
        ),
    }

    suspicious_cells = []

    for key, value in by_prob_odds.items():
        if value["n"] >= 10:
            suspicious_cells.append({
                "cell": key,
                **value,
            })

    suspicious_cells.sort(
        key=lambda x: (
            x["gap_pt"] is None,
            x["gap_pt"] if x["gap_pt"] is not None else 0,
        )
    )

    return {
        "purpose": (
            "実購入群だけを対象に、確率校正がEV選別後も維持されているか確認する"
        ),
        "since": since,
        "overall": overall,
        "odds_drift_detail": odds_drift_detail,
        "by_probability_band": by_prob,
        "by_probability_x_odds": by_prob_odds,
        "by_ev_x_odds": by_ev_odds,
        "most_overpredicted_cells": suspicious_cells[:20],
        "race_level_reference": race_summary,
        "interpretation": {
            "gap_pt_negative": (
                "実績的中率が予測確率を下回り、確率が過大評価されている方向"
            ),
            "p_value_lower_tail_pct": (
                "予測確率が正しい場合に、今回以下の的中数になる片側確率。"
                "小さいほど偶然だけでは説明しにくい"
            ),
            "important": (
                "全体の確率格子が一致していても、"
                "EV上位選別後の購入群で一致するとは限らない"
            ),
        },
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
