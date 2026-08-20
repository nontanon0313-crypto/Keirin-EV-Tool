#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
競輪データ取得 CLI（Odds Park）

使い方:
  # 1レース
  python run_keirin.py --date 20260818 --jo 44 --race 12

  # 1日分（1〜12R）
  python run_keirin.py --date 20260818 --jo 44 --all

  # 複数日
  python run_keirin.py --dates 20260817,20260818 --jo 44 --all

  # 未取得のみ再実行（既存JSONはスキップ）
  python run_keirin.py --dates 20260817,20260818 --jo 44 --all --skip-done
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

# 同梱モジュール
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keirin_oddspark_scraper import scrape_one_race, OUTPUT_DIR


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    log_path = os.path.join(OUTPUT_DIR, "batch_log.txt")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def already_done(date: str, jo: str, race: int) -> bool:
    path = os.path.join(OUTPUT_DIR, f"{date}_{jo}_{race:02d}.json")
    return os.path.exists(path) and os.path.getsize(path) > 1000


def run_one(date: str, jo: str, race: int, skip_done: bool) -> bool:
    if skip_done and already_done(date, jo, race):
        log(f"SKIP {date} jo={jo} R{race:02d}")
        return True
    log(f">>> {date} jo={jo} R{race:02d}")
    t0 = time.time()
    try:
        data = scrape_one_race(jo, date, race)
        m = data.get("odds", {}).get("3連単", {}).get("matrix_count", 0)
        riders = len(data.get("entry", {}).get("riders", []))
        log(f"OK  {date} jo={jo} R{race:02d} {time.time()-t0:.0f}s matrix={m} riders={riders}")
        return True
    except Exception as e:
        log(f"FAIL {date} jo={jo} R{race:02d} {time.time()-t0:.0f}s {type(e).__name__}: {e}")
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="Odds Park 競輪スクレイパー")
    p.add_argument("--date", help="開催日 YYYYMMDD（単日）")
    p.add_argument("--dates", help="開催日をカンマ区切り（例: 20260817,20260818）")
    p.add_argument("--jo", default="44", help="場コード（大垣=44）")
    p.add_argument("--race", type=int, help="レース番号 1-12")
    p.add_argument("--all", action="store_true", help="1〜12R すべて")
    p.add_argument("--skip-done", action="store_true", help="取得済みJSONをスキップ")
    p.add_argument("--races", help="レース番号をカンマ区切り（例: 1,2,12）")
    args = p.parse_args()

    dates: list[str] = []
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    elif args.date:
        dates = [args.date]
    else:
        p.error("--date または --dates を指定してください")

    races: list[int] = []
    if args.all:
        races = list(range(1, 13))
    elif args.races:
        races = [int(x) for x in args.races.split(",") if x.strip()]
    elif args.race:
        races = [args.race]
    else:
        p.error("--race / --races / --all のいずれかを指定してください")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log(f"START dates={dates} jo={args.jo} races={races} out={OUTPUT_DIR}")
    ok = fail = 0
    for d in dates:
        for rn in races:
            if run_one(d, args.jo, rn, args.skip_done):
                ok += 1
            else:
                fail += 1
            time.sleep(1.0)
    log(f"DONE ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
