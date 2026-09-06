#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""確定レースを1件ずつ再投票→結果再検証（進捗表示）。

  python3 scraper/replay_settled.py
  python3 scraper/replay_settled.py --since all --limit 5000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests

API = os.environ.get("API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="all", help="all=全確定レース / calibration_switch / ISO")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--bankroll", type=float, default=1_000_000)
    ap.add_argument("--race-ids", default="", help="カンマ区切り。指定時は一覧APIを使わない")
    ap.add_argument("--sleep", type=float, default=0.3, help="1件ごとの間隔秒")
    args = ap.parse_args()

    if args.race_ids.strip():
        ids = [int(x) for x in args.race_ids.split(",") if x.strip()]
    else:
        params = {"limit": args.limit}
        if args.since and args.since != "all":
            params["since"] = args.since
        else:
            params["since"] = "all"
        log(f"対象一覧取得 {API}/races/replay-settled/targets ...")
        r = requests.get(f"{API}/races/replay-settled/targets", params=params, timeout=120)
        r.raise_for_status()
        data = r.json()
        ids = data.get("race_ids") or []
        log(f"対象 {len(ids)} 件 (since={data.get('since')})")

    if not ids:
        log("対象0件。終了")
        return 0

    done = fail = skip = 0
    t0 = time.time()
    for i, rid in enumerate(ids, 1):
        try:
            rr = requests.post(
                f"{API}/races/{rid}/replay-settled",
                params={"bankroll": args.bankroll},
                timeout=180,
            )
            if rr.status_code >= 400:
                fail += 1
                stage = f"http_{rr.status_code}"
                detail = (rr.text or "")[:120]
            else:
                body = rr.json()
                stage = body.get("stage", "?")
                detail = ""
                if stage == "done":
                    done += 1
                    detail = f"items={body.get('plan_items')} buy={body.get('purchases_recorded')}"
                elif stage and stage.startswith("skipped"):
                    skip += 1
                    detail = body.get("message") or stage
                else:
                    fail += 1
                    detail = str(body.get("error") or body)[:120]
        except Exception as e:
            fail += 1
            stage = "exception"
            detail = str(e)[:120]

        elapsed = time.time() - t0
        rate = i / elapsed if elapsed > 0 else 0
        remain = (len(ids) - i) / rate if rate > 0 else 0
        log(
            f"{i}/{len(ids)} race_id={rid} {stage} {detail} | "
            f"done={done} skip={skip} fail={fail} | "
            f"{elapsed:.0f}s経過 残約{remain:.0f}s"
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    log(f"完了 total={len(ids)} done={done} skip={skip} fail={fail} {time.time()-t0:.0f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
