#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
スクレイパーで取得したレースJSONを使って、
「1.データ取得→2.予想→3.投票プラン作成→4.投票→5.結果記録」を
1コマンドで自動実行する。

このスクリプトはTermux(ネットワークがある実機)で実行することを前提としている。
Claude(コーディングサンドボックス)にはRenderバックエンドやGemini APIへの
ネットワークアクセスが無いため、ここでは実行できない。実機で実行してほしい
(のんの要望「1〜5までを行って欲しい」を受けて追加)。

使い方:
  python run_full_pipeline.py --file data/20260818_44_02.json
  python run_full_pipeline.py --dir data/ --skip-imported
  python run_full_pipeline.py --dir data/ --dry-run   # 予想・投票は行わずデータ取得のみ確認

環境変数:
  KEIRIN_API_BASE  … バックエンドのURL(既定: https://keirin-ev-tool.onrender.com)
  KEIRIN_BANKROLL  … 投票プラン作成に使う証拠金(円)。未指定なら/bankroll/currentを使う
"""
import argparse, glob, json, os, re, sys, time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event

API_BASE = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com")
_log_lock = Lock()


def log(msg):
    with _log_lock:  # 並列実行時にログ行が混ざらないようにする
        print(f"[pipeline] {msg}", flush=True)


def warmup_backend():
    """
    Renderの無料プランはしばらくアクセスが無いとスリープし、次のアクセスで
    起動に30秒以上かかることがある。パイプライン開始前に軽いGETリクエストで
    先にウォームアップしておくことで、以降の各ステップのタイムアウトを
    短いままにできる(のんの実機検証でimportがタイムアウトした件を受けて追加)。
    """
    log("バックエンドをウォームアップ中(コールドスタート対策)...")
    try:
        requests.get(f"{API_BASE}/docs", timeout=90)
        log("  ウォームアップ完了")
    except Exception as e:
        log(f"  ウォームアップ失敗(続行します): {e}")


def _is_transient_network_error(e):
    """
    DNS引けない・接続が途中で切れた等、時間を置けば直る可能性が高いエラーかを判定する
    (のんの実機運用で、Termuxがバックグラウンドに回った際にネットが一時的に
    切れる現象が頻発したため追加)。
    """
    msg = str(e)
    return any(
        s in msg
        for s in (
            "NameResolutionError", "Failed to resolve",
            "Connection aborted", "ConnectionError",
            "Max retries exceeded", "Read timed out", "ConnectionResetError",
        )
    )


def _post_with_retry(url, **kwargs):
    """一時的なネットワークエラーは数秒待って最大3回までリトライする"""
    last_err = None
    for attempt in range(3):
        try:
            return requests.post(url, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < 2 and _is_transient_network_error(e):
                wait = 5 * (attempt + 1)
                log(f"   一時的な通信エラーのため{wait}秒待って再試行します({attempt + 1}/2回目): {e}")
                time.sleep(wait)
            else:
                raise
    raise last_err


def step1_import(payload):
    """1. データ取得(登録): スクレイパーJSONをバックエンドに取り込む"""
    r = _post_with_retry(f"{API_BASE}/scraper-import/race", json=payload, timeout=90)
    if r.status_code >= 400:
        # 以前はエラーの中身(なぜダメだったか)が見えず原因調査に手間取っていたため、
        # サーバーから返ってきた理由をログに出すようにした(のんの実機運用で判明・修正)。
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        log(f"   → サーバーからの理由: {detail}")
    r.raise_for_status()
    return r.json()


def reset_race(race_id):
    """
    出走表・オッズ・結果は残したまま、予想関連データ(AI勝率・購入・見送り・EV結果)
    だけを削除する。予想ロジックを変えて再検証したい時、スクレイピングをやり直さずに
    使う(のんの要望により追加)。
    """
    r = requests.post(f"{API_BASE}/races/{race_id}/reset-for-reanalysis", timeout=90)
    r.raise_for_status()
    return r.json()


def get_race(race_id):
    r = requests.get(f"{API_BASE}/races/{race_id}", timeout=90)
    r.raise_for_status()
    return r.json()


class RateLimitExhausted(Exception):
    """Geminiの利用枠が(一時的リトライでは解消しない程度に)尽きたと判断した時に送出する。
    このエラーが出たら、そのレースだけでなく処理全体を止める(のんの要望により追加)。"""
    pass


def step2_estimate(race_id, max_retries=5):
    """
    2. 予想: Gemini 2段階分析(展開シミュレーション→勝率推定)。

    Geminiの利用枠(RPM/RPD)を超えると429が返ってくることがある。データが
    壊れるわけではないので、まず待ってから自動リトライする。それでも
    max_retries回連続で429が続く場合は「一時的な混雑ではなく、日次上限等に
    達した可能性が高い」と判断し、RateLimitExhaustedを送出して処理全体を
    止める(のんの要望により追加。実際の枠はGoogle AI Studio等で要確認、
    ここでは上限の値は決め打ちしない)。
    """
    url = f"{API_BASE}/analyze/estimate/{race_id}"
    for attempt in range(max_retries):
        r = requests.post(url, timeout=120)
        if r.status_code == 429:
            wait = min(10 * (2 ** attempt), 120)
            log(f"   race_id={race_id}: Gemini利用枠の制限(429)。{wait}秒待って再試行します({attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RateLimitExhausted(f"race_id={race_id}: 429が{max_retries}回連続しました。利用枠(日次上限等)に達した可能性があります")


def step3_race_plan(race_id, bankroll):
    """3. 投票プラン作成: EV計算・ステーキング(bankroll未指定なら証拠金残高を自動使用)"""
    body = {"race_id": race_id}
    if bankroll is not None:
        body["bankroll"] = bankroll
    r = requests.post(f"{API_BASE}/ev/race-plan/{race_id}", json=body, timeout=90)
    r.raise_for_status()
    return r.json()


def step4_record_purchases(race_id, plan):
    """4. 投票: 投票プランの内容をPurchaseとして記録する(実際の車券購入はしない。記録のみ)"""
    items = plan.get("items", [])
    if not items:
        return {"recorded": 0}
    body = {
        "items": [
            {
                "race_id": race_id,
                "bet_type": it["bet_type"],
                "combination": it["combination"],
                "stake_amount": it["stake"],
                "odds_at_purchase": it.get("odds_value"),
                "win_prob_at_purchase": it["win_prob"] if "win_prob" in it else it.get("estimated_win_prob_pct", 0) / 100,
                "win_prob_raw": it.get("win_prob_raw"),
                "ev_pct_at_purchase": it.get("ev_pct"),
            }
            for it in items
        ],
    }
    r = requests.post(f"{API_BASE}/purchases/bulk", json=body, timeout=90)
    r.raise_for_status()
    return {"recorded": len(items), "response": r.json()}


def _extract_actual_result(race_json):
    """
    スクレイパーが取得したJSONから、確定済みの1-2-3着(車番)を取り出す。
    まだレースが行われていない・着順が取得できていない場合はNoneを返す。
    車番が数字1桁でない行(選手名ずれ等)は無効としてNoneを返す。
    """
    results = race_json.get("result", {}).get("results", [])
    by_place = {}
    for r in results:
        place = str(r.get("着順") or "").strip()
        car = str(r.get("車番") or "").strip()
        if place in ("1", "2", "3") and re.fullmatch(r"[1-9]", car):
            by_place[place] = car
    if not all(p in by_place for p in ("1", "2", "3")):
        return None
    return f"{by_place['1']}-{by_place['2']}-{by_place['3']}"


def get_current_bankroll():
    r = requests.get(f"{API_BASE}/bankroll/", timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("current_balance")


def run_one_race(race_json, bankroll, dry_run=False):
    label = f"{race_json.get('kaisai_bi')}_{race_json.get('jo_code')}_{race_json.get('race_no')}"
    log(f"=== {label} ===")

    log("1. データ取得(登録)...")
    imp = step1_import(race_json)
    race_id = imp.get("race_id")

    if imp.get("skipped"):
        # 出走選手がいない(そのjo/rnにレースが存在しない)枠。以前はここで
        # entries_created等の存在しないキーを直読みしてKeyErrorになっていた
        # (のんの実機運用で判明した不具合を受けて修正)。
        log(f"   スキップ: {imp.get('reason', '出走表データが無いため')}")
        return {"race_id": race_id, "stage": "skipped_empty"}

    log(f"   race_id={race_id} venue={imp.get('venue_name')} entries+={imp.get('entries_created', 0)} odds+={imp.get('odds_created', 0)}")
    if imp.get("warnings"):
        for w in imp["warnings"]:
            log(f"   警告: {w}")

    if dry_run:
        log("   --dry-run のためここで終了(予想は実行していません)")
        return {"race_id": race_id, "stage": "imported_only"}

    if imp.get("entries_created", 0) == 0 and imp.get("entries_updated", 0) == 0:
        log("   出走表データが無いため予想をスキップします")
        return {"race_id": race_id, "stage": "no_entries"}

    log("   (--dry-run無し: このまま予想〜結果確定まで実行します)")
    # バグ修正: 以前はここでスクレイパーJSON内の着順を一切見ておらず、
    # 新規データの通常経路(--dir/--file)では結果が取れていても常に
    # 「未確定」のままになっていた(のんの指摘により修正)。
    actual_result = _extract_actual_result(race_json)
    return run_predict_and_confirm(race_id, bankroll, actual_result=actual_result)


FIXED_BANKROLL = 1_000_000  # 検証・集計目的の投票プランは常にこの額を使う(実際の証拠金残高とは無関係)


def run_reanalysis_for_race(race_id, bankroll=None, reset=True):
    """
    既にDBに登録済みのレース(出走表・オッズ・結果あり)を、スクレイピングし直さずに
    再予想する。予想ロジック(AIプロンプト等)を変更した後の再検証用。
    """
    label = f"race_id={race_id}"
    log(f"=== {label}(再予想) ===")

    if reset:
        log("0. 予想関連データをリセット(出走表・オッズ・結果は保持)...")
        r = reset_race(race_id)
        log(f"   購入{r['purchases_deleted']}件・見送り{r['skipped_bets_deleted']}件・EV結果{r['ev_results_deleted']}件を削除")

    race = get_race(race_id)
    if not race.get("actual_result"):
        log("   このレースはまだ結果が確定していません。結果記録(手順5)はスキップします")

    result = run_predict_and_confirm(race_id, FIXED_BANKROLL, actual_result=race.get("actual_result"))
    return result


def run_predict_and_confirm(race_id, bankroll, actual_result=None):
    """
    2〜5. 予想→投票プラン作成→投票→結果記録(race_json不要版)。

    証拠金は実際の残高を読み書きせず、呼び出し元が渡した固定額(検証・集計目的なら
    FIXED_BANKROLL)をそのまま計算に使うだけなので、複数レースを同時実行しても
    競合しない(以前は実際の残高を読み書きしていたため、複数レース同時実行時に
    上限を超えて使われる不具合があったが、証拠金を実際に増減させない設計に
    変更したことで根本的に解消した。のんの要望により修正)。
    """
    log("2. 予想(Gemini 2段階分析)...")
    try:
        est = step2_estimate(race_id)
        log(f"   完了: {est.get('updated_entries', 0)}名分の勝率を推定")
    except Exception as e:
        log(f"   予想に失敗しました: {e}")
        return {"race_id": race_id, "stage": "estimate_failed", "error": str(e)}

    log("3. 投票プラン作成...")
    plan = step3_race_plan(race_id, bankroll)
    if plan.get("skipped_no_odds"):
        log("   オッズデータが無いためスキップします")
        return {"race_id": race_id, "stage": "skipped_no_odds"}
    n_items = len(plan.get("items", []))
    log(f"   買い示唆 {n_items}件 (総額{plan.get('total_stake', 0)}円)")

    log("4. 投票(記録)...")
    rec = step4_record_purchases(race_id, plan)
    log(f"   記録件数: {rec.get('recorded', n_items)}")

    if not actual_result:
        log("5. 結果記録...スキップ(結果未確定)")
        return {"race_id": race_id, "stage": "predicted_no_result"}

    log("5. 結果記録...")
    r = requests.post(f"{API_BASE}/races/{race_id}/confirm-result", params={"actual_result": actual_result}, timeout=90)
    r.raise_for_status()
    conf = r.json()
    log(f"   確定: {conf.get('actual_result', conf)}")

    return {"race_id": race_id, "stage": "done"}


PROGRESS_DEFAULT_PATH = "pipeline_progress.json"
_stop_event = Event()  # Gemini利用枠切れを検知したら立てる。以降の新規タスク開始を止める


def load_progress(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress(path, progress):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def _lightweight_confirm_sweep(files):
    """
    再予想(Gemini呼び出し)は一切行わず、結果確定だけを毎回試みる。
    進捗ファイルによってスキップされた「以前処理済みだが当時は結果が
    出ていなかった」レースも、ここで毎回確認することで、次に実行した
    ときに自動で結果登録されるようにする
    (のんの指摘「run_day.shを使った時に結果まで登録してあってほしい」により追加)。
    再登録(scraper-import)・結果確定(confirm-result)はどちらも何度呼んでも
    安全な作り(冪等)になっているので、既に確定済みのレースに対して呼んでも害はない。
    """
    confirmed, still_pending, error = 0, 0, 0
    total = len(files)
    for i, fp in enumerate(files, 1):
        if i == 1 or i % 20 == 0 or i == total:
            log(f"   結果確定スイープ進捗: {i}/{total}件目")
        try:
            with open(fp, encoding="utf-8") as f:
                race_json = json.load(f)
            actual_result = _extract_actual_result(race_json)
            if not actual_result:
                still_pending += 1
                continue
            imp = step1_import(race_json)
            race_id = imp.get("race_id")
            if not race_id:
                continue
            r = _post_with_retry(
                f"{API_BASE}/races/{race_id}/confirm-result",
                params={"actual_result": actual_result},
                timeout=90,
            )
            r.raise_for_status()
            confirmed += 1
        except Exception as e:
            log(f"   結果確定スイープでエラー({os.path.basename(fp)}): {e}")
            error += 1
    log(f"--- 結果確定スイープ: 確定/確認{confirmed}件・結果まだ無し{still_pending}件・エラー{error}件 ---")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="1レース分のJSONファイル")
    ap.add_argument("--dir", help="複数レース分のJSONファイルが入ったディレクトリ")
    ap.add_argument("--date", help="YYYYMMDD。--dir指定時にこの開催日のファイルだけ処理する")
    ap.add_argument("--race-ids", help="既存のrace_id(カンマ区切り)を再予想する。スクレイピングは行わない(例: 46,47,48)")
    ap.add_argument("--no-reset", action="store_true", help="--race-ids使用時、リセットせずに(=既存の購入記録に追加する形で)再予想する。通常は指定しない")
    ap.add_argument("--bankroll", type=float, default=None, help="証拠金(円)。未指定なら固定100万円を使用(検証・集計目的のため)")
    ap.add_argument("--dry-run", action="store_true", help="データ取得(登録)までで止める")
    ap.add_argument("--concurrency", type=int, default=1, help="同時に処理するレース数(既定1=逐次)。Geminiの利用枠に応じて調整してください")
    ap.add_argument("--progress-file", default=PROGRESS_DEFAULT_PATH, help=f"進捗記録ファイル(既定: {PROGRESS_DEFAULT_PATH})。既に成功済みのタスクは自動でスキップし、Gemini利用枠切れで停止した後の再実行では続きから再開する")
    args = ap.parse_args()

    warmup_backend()

    bankroll = args.bankroll
    if bankroll is None and not args.dry_run:
        log(f"証拠金(--dir/--file用)は未指定のため、固定{FIXED_BANKROLL:,}円を使用します(検証・集計目的のため実際の残高は使いません)")
        bankroll = FIXED_BANKROLL

    progress = load_progress(args.progress_file)
    summary = []
    stopped_for_rate_limit = False

    # 成功とみなす("done"扱いにして次回スキップする)ステージ一覧。
    # エラー・レート制限切れは含めない(次回また試すため)。
    # "imported_only"(--dry-runで意図的にそこで止めた状態)は含めない。
    # 以前は含めていたため、--dry-runで一度動かした後にdry-run無しで
    # 再実行すると、登録済みだった分が「もう完了済み」と誤認され、
    # 一度も予想が実行されないまま進捗ファイル上だけ完了扱いになっていた
    # (のんの実機運用で判明した不具合を受けて修正)。
    SUCCESS_STAGES = {"done", "predicted_no_result", "no_entries", "skipped_no_odds", "skipped_empty"}

    def run_with_summary(task_key, task_label, fn):
        nonlocal stopped_for_rate_limit
        if _stop_event.is_set():
            return
        if progress.get(task_key) == "done":
            log(f"   スキップ({task_label}): 進捗ファイルに完了記録あり")
            return
        try:
            result = fn()
            summary.append({"file": task_label, **result})
            if not args.dry_run and result.get("stage") in SUCCESS_STAGES:
                progress[task_key] = "done"
                save_progress(args.progress_file, progress)
        except RateLimitExhausted as e:
            log(f"   停止(Gemini利用枠切れ): {e}")
            log("   処理全体を停止します。利用枠が回復してから、同じコマンドをもう一度実行してください(完了済みの分は自動でスキップされます)")
            _stop_event.set()
            stopped_for_rate_limit = True
            summary.append({"file": task_label, "stage": "rate_limit_stopped", "error": str(e)})
        except Exception as e:
            log(f"   エラー({task_label}): {e}")
            summary.append({"file": task_label, "stage": "error", "error": str(e)})

    if args.race_ids:
        race_ids = [int(x.strip()) for x in args.race_ids.split(",") if x.strip()]
        log(f"対象レース数: {len(race_ids)}件(同時実行数: {args.concurrency}, 証拠金: 固定{FIXED_BANKROLL:,}円)")
        if args.concurrency <= 1:
            for rid in race_ids:
                if _stop_event.is_set():
                    break
                run_with_summary(f"reanalysis:{rid}", f"race_id={rid}", lambda rid=rid: run_reanalysis_for_race(rid, reset=not args.no_reset))
                time.sleep(0.3)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futs = {}
                skipped_count = 0
                for rid in race_ids:
                    if _stop_event.is_set():
                        break
                    if progress.get(f"reanalysis:{rid}") == "done":
                        skipped_count += 1
                        continue
                    futs[ex.submit(run_reanalysis_for_race, rid, None, not args.no_reset)] = rid
                if skipped_count:
                    log(f"({skipped_count}件は進捗ファイルに完了記録があるためスキップします)")
                for fut in as_completed(futs):
                    rid = futs[fut]
                    run_with_summary(f"reanalysis:{rid}", f"race_id={rid}", lambda fut=fut: fut.result())
        log("=== サマリ ===")
        for s in summary:
            log(f"  {s['file']}: {s['stage']}")
        if stopped_for_rate_limit:
            log("(Gemini利用枠切れで途中停止しました。回復後に同じコマンドで再実行してください)")
            sys.exit(2)
        return

    files = []
    if args.file:
        files = [args.file]
    elif args.dir:
        all_json = glob.glob(os.path.join(args.dir, "*.json"))
        race_file_re = re.compile(r"^\d{8}_\d+_\d{2}\.json$")
        files = sorted(f for f in all_json if race_file_re.match(os.path.basename(f)))
        excluded = len(all_json) - len(files)
        if excluded:
            log(f"({excluded}件のファイルはレースJSONの命名パターン(YYYYMMDD_jo_RR.json)に一致しないため除外しました。例: status.json, debug_*.json)")
        if getattr(args, "date", None):
            prefix = args.date
            before = len(files)
            files = [f for f in files if os.path.basename(f).startswith(prefix + "_")]
            log(f"(--date {prefix}: {before}件中{len(files)}件に絞り込み)")
    else:
        ap.error("--file か --dir か --race-ids のいずれかを指定してください")

    log(f"対象ファイル数: {len(files)}件(同時実行数: {args.concurrency})")
    if args.concurrency <= 1:
        for fp in files:
            if _stop_event.is_set():
                break
            with open(fp, encoding="utf-8") as f:
                race_json = json.load(f)
            run_with_summary(f"file:{fp}", fp, lambda race_json=race_json: run_one_race(race_json, bankroll, dry_run=args.dry_run))
            time.sleep(0.3)
    else:
        def load_and_run(fp):
            with open(fp, encoding="utf-8") as f:
                race_json = json.load(f)
            return run_one_race(race_json, bankroll, dry_run=args.dry_run)

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {}
            skipped_count = 0
            for fp in files:
                if _stop_event.is_set():
                    break
                if progress.get(f"file:{fp}") == "done":
                    skipped_count += 1
                    continue
                futs[ex.submit(load_and_run, fp)] = fp
            if skipped_count:
                log(f"({skipped_count}件は進捗ファイルに完了記録があるためスキップします)")
            for fut in as_completed(futs):
                fp = futs[fut]
                run_with_summary(f"file:{fp}", fp, lambda fut=fut: fut.result())

    if not args.dry_run:
        log("--- 結果確定スイープ(再予想なしで、結果が出ているレースだけ確定) ---")
        _lightweight_confirm_sweep(files)

    log("=== サマリ ===")
    for s in summary:
        log(f"  {os.path.basename(s['file'])}: {s['stage']}")
    if stopped_for_rate_limit:
        log("(Gemini利用枠切れで途中停止しました。回復後に同じコマンドで再実行してください)")
        sys.exit(2)


if __name__ == "__main__":
    main()
