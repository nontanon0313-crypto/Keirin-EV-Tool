#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""プラン未作成レースを処理。日付範囲対応。"""
from __future__ import annotations
import argparse, os, time
import requests

API = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")
BANKROLL = float(os.environ.get("KEIRIN_BANKROLL", "1000000"))
BATCH = int(os.environ.get("KEIRIN_BATCH", "100"))
_DB_TIER = (os.environ.get("KEIRIN_DB_TIER") or "").strip().lower()
_HEADERS = {"X-Keirin-DB-Tier": _DB_TIER} if _DB_TIER else {}

def log(msg): print(msg, flush=True)

def get_json(path, params=None, timeout=120):
    r = requests.get(f"{API}{path}", params=params or {}, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def post_json(path, payload=None, timeout=300):
    r = requests.post(f"{API}{path}", json=payload or {},
                      headers={**_HEADERS, "Content-Type": "application/json"}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    return r.json()

def coverage():
    try:
        data = get_json("/races/coverage-by-month", timeout=180)
    except Exception as e:
        log(f"coverage-by-month 失敗(未デプロイの可能性): {e}")
        return
    log(f"calibration_switch_at={data.get('calibration_switch_at')}")
    log(f"{'month':<8} {'total':>6} {'set':>5} {'unset':>5} {'ai':>5} {'odds':>5} {'purch':>6} {'skip':>5} {'missPlan':>8} {'replayEl':>8}")
    for m in data.get("months") or []:
        log(f"{m.get('month','?'):<8} {m.get('total',0):>6} {m.get('settled',0):>5} {m.get('unsettled',0):>5} "
            f"{m.get('has_ai',0):>5} {m.get('has_trifecta_odds',0):>5} {m.get('has_purchase',0):>6} "
            f"{m.get('has_skipped',0):>5} {m.get('missing_plan_eligible',0):>8} {m.get('replay_eligible',0):>8}")

def process_settled(args):
    params = {"since": args.date_from or "all", "limit": args.batch,
              "exclude_already_replayed": "true" if args.exclude_already_replayed else "false"}
    if args.date_to:
        params["until"] = args.date_to
    log(f"確定済み targets: {params}")
    data = get_json("/races/replay-settled/targets", params=params, timeout=180)
    ids = data.get("race_ids") or []
    log(f"  total={data.get('total')} 今回={len(ids)}")
    ok = 0
    for rid in ids:
        try:
            log(f"replay-settled race_id={rid}")
            r = post_json(f"/races/{rid}/replay-settled", {"bankroll": args.bankroll}, timeout=300)
            log(f"  OK {rid}")
            ok += 1
        except Exception as e:
            log(f"  FAIL {rid}: {e}")
        time.sleep(args.sleep)
    return ok

def process_unsettled(args):
    params = {"limit": args.batch, "include_today": "false"}
    if args.date_from: params["date_from"] = args.date_from
    if args.date_to: params["date_to"] = args.date_to
    log(f"未確定 targets: {params}")
    data = get_json("/races/missing-plan/targets", params=params, timeout=120)
    races = data.get("races") or []
    log(f"  total={data.get('total')} 今回={len(races)}")
    ok = 0
    for row in races:
        rid = row["id"]
        try:
            log(f"race-plan {rid} {row.get('race_date')} {row.get('venue_name')} {row.get('race_number')}R")
            get_json(f"/ev/race-plan/{rid}", params={"bankroll": args.bankroll}, timeout=180)
            log(f"  OK {rid}")
            ok += 1
        except Exception as e:
            log(f"  FAIL {rid}: {e}")
        time.sleep(args.sleep)
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default=None)
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--bankroll", type=float, default=BANKROLL)
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--exclude-already-replayed", default="true", choices=["true", "false"])
    ap.add_argument("--coverage-only", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=3)
    args = ap.parse_args()
    args.exclude_already_replayed = args.exclude_already_replayed == "true"
    log(f"API={API} BANKROLL={args.bankroll:,.0f} BATCH={args.batch}")
    coverage()
    if args.coverage_only:
        return
    for i in range(1, args.max_rounds + 1):
        log(f"--- {i}周目 ---")
        n1 = process_settled(args)
        n2 = process_unsettled(args)
        log(f"--- {i}周目: 確定{n1} / 未確定{n2} ---")
        if n1 == 0 and n2 == 0:
            break
    log("=== 完了 ===")

if __name__ == "__main__":
    main()
