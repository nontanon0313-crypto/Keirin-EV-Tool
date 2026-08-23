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
import argparse, glob, json, os, sys, time
import requests

API_BASE = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com")


def log(msg):
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


def step1_import(payload):
    """1. データ取得(登録): スクレイパーJSONをバックエンドに取り込む"""
    r = requests.post(f"{API_BASE}/scraper-import/race", json=payload, timeout=90)
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


def step2_estimate(race_id):
    """2. 予想: Gemini 2段階分析(展開シミュレーション→勝率推定)"""
    r = requests.post(f"{API_BASE}/analyze/estimate/{race_id}", timeout=120)
    r.raise_for_status()
    return r.json()


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
                "ev_pct_at_purchase": it.get("ev_pct"),
            }
            for it in items
        ],
    }
    r = requests.post(f"{API_BASE}/purchases/bulk", json=body, timeout=90)
    r.raise_for_status()
    return {"recorded": len(items), "response": r.json()}


def step5_confirm_result(race_id, race_json):
    """5. 結果記録: スクレイパーが取得済みの着順を使って自動確定する"""
    results = race_json.get("result", {}).get("results", [])
    by_place = {r["着順"]: r["車番"] for r in results}
    parts = [by_place[str(p)] for p in (1, 2, 3) if str(p) in by_place]
    if len(parts) != 3:
        log(f"  結果データが不完全なため結果記録をスキップします: {by_place}")
        return None
    actual_result = "-".join(parts)
    r = requests.post(
        f"{API_BASE}/races/{race_id}/confirm-result",
        params={"actual_result": actual_result},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()


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
    race_id = imp["race_id"]
    log(f"   race_id={race_id} venue={imp['venue_name']} entries+={imp['entries_created']} odds+={imp['odds_created']}")
    if imp.get("warnings"):
        for w in imp["warnings"]:
            log(f"   警告: {w}")

    if dry_run:
        log("   --dry-run のためここで終了")
        return {"race_id": race_id, "stage": "imported_only"}

    if imp["entries_created"] == 0 and imp["entries_updated"] == 0:
        log("   出走表データが無いため予想をスキップします")
        return {"race_id": race_id, "stage": "no_entries"}

    return run_predict_and_confirm(race_id, bankroll)


def run_reanalysis_for_race(race_id, bankroll, reset=True):
    """
    既にDBに登録済みのレース(出走表・オッズ・結果あり)を、スクレイピングし直さずに
    再予想する。予想ロジック(AIプロンプト等)を変更した後の再検証用
    (のんの要望により追加)。
    """
    label = f"race_id={race_id}"
    log(f"=== {label}(再予想) ===")

    if reset:
        log("0. 予想関連データをリセット(出走表・オッズ・結果は保持)...")
        r = reset_race(race_id)
        log(f"   購入{r['purchases_deleted']}件・見送り{r['skipped_bets_deleted']}件・EV結果{r['ev_results_deleted']}件を削除、証拠金を{r['bankroll_adjustment']:+.0f}円調整")

    race = get_race(race_id)
    if not race.get("actual_result"):
        log("   このレースはまだ結果が確定していません。結果記録(手順5)はスキップします")

    result = run_predict_and_confirm(race_id, bankroll, actual_result=race.get("actual_result"))
    return result


def run_predict_and_confirm(race_id, bankroll, actual_result=None):
    """2〜5. 予想→投票プラン作成→投票→結果記録(race_json不要版)"""
    log("2. 予想(Gemini 2段階分析)...")
    try:
        est = step2_estimate(race_id)
        log(f"   完了: {est.get('updated_entries', 0)}名分の勝率を推定")
    except Exception as e:
        log(f"   予想に失敗しました: {e}")
        return {"race_id": race_id, "stage": "estimate_failed", "error": str(e)}

    log("3. 投票プラン作成...")
    plan = step3_race_plan(race_id, bankroll)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="1レース分のJSONファイル")
    ap.add_argument("--dir", help="複数レース分のJSONファイルが入ったディレクトリ")
    ap.add_argument("--race-ids", help="既存のrace_id(カンマ区切り)を再予想する。スクレイピングは行わない(例: 46,47,48)")
    ap.add_argument("--no-reset", action="store_true", help="--race-ids使用時、リセットせずに(=既存の購入記録に追加する形で)再予想する。通常は指定しない")
    ap.add_argument("--bankroll", type=float, default=None, help="証拠金(円)。未指定ならバックエンドの現在値を使用")
    ap.add_argument("--dry-run", action="store_true", help="データ取得(登録)までで止める")
    args = ap.parse_args()

    warmup_backend()

    bankroll = args.bankroll
    if bankroll is None and not args.dry_run:
        log("証拠金は未指定のため、バックエンド側の現在の証拠金残高を自動使用します")

    summary = []

    if args.race_ids:
        race_ids = [int(x.strip()) for x in args.race_ids.split(",") if x.strip()]
        for rid in race_ids:
            try:
                result = run_reanalysis_for_race(rid, bankroll, reset=not args.no_reset)
                summary.append({"file": f"race_id={rid}", **result})
            except Exception as e:
                log(f"   エラー: {e}")
                summary.append({"file": f"race_id={rid}", "stage": "error", "error": str(e)})
            time.sleep(0.5)
        log("=== サマリ ===")
        for s in summary:
            log(f"  {s['file']}: {s['stage']}")
        return

    files = []
    if args.file:
        files = [args.file]
    elif args.dir:
        files = sorted(glob.glob(os.path.join(args.dir, "*.json")))
    else:
        ap.error("--file か --dir か --race-ids のいずれかを指定してください")

    for fp in files:
        with open(fp, encoding="utf-8") as f:
            race_json = json.load(f)
        try:
            result = run_one_race(race_json, bankroll, dry_run=args.dry_run)
            summary.append({"file": fp, **result})
        except Exception as e:
            log(f"   エラー: {e}")
            summary.append({"file": fp, "stage": "error", "error": str(e)})
        time.sleep(0.5)

    log("=== サマリ ===")
    for s in summary:
        log(f"  {os.path.basename(s['file'])}: {s['stage']}")


if __name__ == "__main__":
    main()
