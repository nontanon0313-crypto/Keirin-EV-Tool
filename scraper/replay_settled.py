#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""確定済みレースの再投票→結果再検証を一括実行する。

使い方:
  python scraper/replay_settled.py
  python scraper/replay_settled.py --since calibration_switch --limit 80
  python scraper/replay_settled.py --race-ids 10,11,12
"""
import argparse
import json
import os
import sys

import requests

API = os.environ.get("API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="all", help="all=全確定レース")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--bankroll", type=float, default=1_000_000)
    ap.add_argument("--race-ids", default="", help="カンマ区切り。指定時はsinceより優先")
    args = ap.parse_args()

    body = {
        "limit": args.limit,
        "bankroll": args.bankroll,
    }
    if args.since and args.since != "all":
        body["since"] = args.since
    if args.race_ids.strip():
        body["race_ids"] = [int(x) for x in args.race_ids.split(",") if x.strip()]

    print(f"POST {API}/races/replay-settled/batch ...")
    r = requests.post(f"{API}/races/replay-settled/batch", json=body, timeout=600)
    print("status", r.status_code)
    try:
        data = r.json()
    except Exception:
        print(r.text[:3000])
        sys.exit(1)
    print(json.dumps({k: data[k] for k in data if k != "results"}, ensure_ascii=False, indent=2))
    fails = [x for x in data.get("results", []) if x.get("stage") != "done"]
    if fails:
        print("--- not done (up to 20) ---")
        for x in fails[:20]:
            print(x)
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()
