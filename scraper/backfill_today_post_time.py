#!/usr/bin/env python3
"""
発走時刻(post_time)がNULLのまま登録されてしまった当日レースを埋める。

parse_entry自体に発走時刻取得が無かった不具合(2026-09-06修正)により、
それ以前に取り込んだレースはpost_timeがNULLのまま残っている。
このスクリプトはオッズ・出走表の再取得はせず、RaceList.doから発走時刻だけを
軽量に取り直し、/races/backfill-post-time にまとめて送る。

使い方:
  python3 scraper/backfill_today_post_time.py --date 20260906
  python3 scraper/backfill_today_post_time.py   # 省略時はJST今日
"""
from __future__ import annotations
import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from keirin_oddspark_scraper import get_soup, BASE  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
API_BASE = os.environ.get("API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")

# 場コード確定情報(app/keirin_data.pyと同じ)
JO_CODES = (
    list(range(11, 14)) + list(range(21, 29)) +
    [31, 32] + list(range(34, 39)) +
    list(range(41, 49)) +
    [51] + list(range(53, 57)) +
    list(range(61, 64)) +
    [71] + list(range(73, 76)) +
    [81] + list(range(83, 88))
)


def parse_post_time(jo, kaisai_bi, race_no):
    soup = get_soup(f"{BASE}/RaceList.do", {"joCode": jo, "kaisaiBi": kaisai_bi, "raceNo": race_no})
    text = soup.get_text(" ", strip=True)
    m = re.search(r"発走時間\s*(\d{1,2}:\d{2})", text)
    if not m:
        return None
    hh, mm = map(int, m.group(1).split(":"))
    y, mo, d = int(kaisai_bi[:4]), int(kaisai_bi[4:6]), int(kaisai_bi[6:8])
    return datetime(y, mo, d, hh, mm).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD(省略時はJST今日)")
    args = ap.parse_args()
    today = args.date or datetime.now(JST).strftime("%Y%m%d")

    print(f"対象日: {today}")
    races = requests.get(f"{API_BASE}/races/today", timeout=30).json()
    # race_dateは/races/today側では絞り込み済みなので、venue_name/race_numberから
    # joCodeを逆引きしつつ、post_timeが無いものだけ対象にする
    targets = [r for r in races if r.get("post_time") is None]
    print(f"post_time未設定: {len(targets)}件 / 全体{len(races)}件")
    if not targets:
        print("対象なし。終了します。")
        return

    # venue_name -> jo_code の対応をBankMaster経由で引けないため、
    # RaceListInfo.doから当日開催のjoCode一覧を取り、名称マッチで引く。
    # 会場ごとに1回だけjoCodeを特定してから、各レースはparse_post_timeを
    # 1回ずつ呼ぶだけにする(以前の実装は毎レースごとに全joCodeへ照合リクエストを
    # 送っていて無駄が多かったため2026-09-06に効率化)。
    r = requests.get(
        "https://www.oddspark.com/keirin/RaceListInfo.do",
        params={"kaisaiBi": today},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    jos_today = sorted(set(re.findall(r"joCode=(\d+)", r.text)))
    print(f"本日開催joCode: {jos_today}")

    venues_needed = sorted({t["venue_name"] for t in targets})
    venue_to_jo = {}
    for venue in venues_needed:
        for jo in jos_today:
            try:
                soup = get_soup(f"{BASE}/RaceList.do", {"joCode": jo, "kaisaiBi": today, "raceNo": 1})
            except Exception:
                continue
            title = soup.title.string if soup.title else ""
            if venue in (title or ""):
                venue_to_jo[venue] = jo
                break
        time.sleep(0.3)
    print(f"会場→joCode対応: {venue_to_jo}")

    items = []
    for race in targets:
        venue = race["venue_name"]
        race_no = race["race_number"]
        jo = venue_to_jo.get(venue)
        if not jo:
            print(f"  スキップ(joCode特定失敗): {venue} {race_no}R")
            continue
        iso = parse_post_time(jo, today, race_no)
        if iso:
            items.append({"race_id": race["race_id"], "post_time_iso": iso})
            print(f"  OK {venue} {race_no}R 発走={iso}")
        else:
            print(f"  取得失敗: {venue} {race_no}R")
        time.sleep(0.5)

    if not items:
        print("バックフィル対象なし(取得失敗のみ)。終了します。")
        return

    resp = requests.post(f"{API_BASE}/races/backfill-post-time", json=items, timeout=60)
    resp.raise_for_status()
    print("バックフィル結果:", resp.json())


if __name__ == "__main__":
    main()
