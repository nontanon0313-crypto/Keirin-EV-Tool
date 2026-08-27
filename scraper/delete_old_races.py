#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定した開催日(--keep)以外のレースを、購入・見送り・EV結果・出走表・オッズも
含めてDBから削除する。

デフォルトはドライラン(削除対象の件数だけ表示)。実際に消すときは --yes を付ける。

使い方:
  cd ~/Keirin-EV-Tool/scraper

  # まず確認だけ(何も消さない)
  python delete_old_races.py --keep 08/25,08/26

  # 内容を確認した上で実際に削除
  python delete_old_races.py --keep 08/25,08/26 --yes
"""
from __future__ import annotations
import argparse, os
import requests

API_BASE = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", required=True, help="残す開催日をMM/DD形式でカンマ区切り(例: 08/25,08/26)")
    ap.add_argument("--yes", action="store_true", help="指定しないとドライラン(表示のみ)")
    args = ap.parse_args()

    keep_dates = {d.strip() for d in args.keep.split(",") if d.strip()}
    print(f"残す開催日: {sorted(keep_dates)}")

    r = requests.get(f"{API_BASE}/races/for-reanalysis", params={"limit": 1000}, timeout=60)
    r.raise_for_status()
    races = r.json()
    print(f"DB上の全レース件数: {len(races)}件")

    targets = [rc for rc in races if rc.get("race_date") not in keep_dates]
    print(f"削除対象: {len(targets)}件")

    from collections import Counter
    by_date = Counter(rc.get("race_date") for rc in targets)
    print(f"削除対象の内訳(開催日別): {dict(by_date)}")

    if not args.yes:
        print("\n--- ドライランのため、まだ何も削除していません ---")
        print("内容を確認し、問題なければ --yes を付けて再実行してください。")
        return

    deleted, error = 0, 0
    for rc in targets:
        try:
            dr = requests.delete(f"{API_BASE}/races/{rc['id']}", timeout=60)
            dr.raise_for_status()
            deleted += 1
        except Exception as e:
            print(f"  エラー(race_id={rc['id']}): {e}")
            error += 1

    print(f"\n=== 完了: 削除{deleted}件 / エラー{error}件 ===")


if __name__ == "__main__":
    main()
