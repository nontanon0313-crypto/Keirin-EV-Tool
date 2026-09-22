#!/usr/bin/env python3
"""開催日の全場×レース の joblist.txt を作成する。

--after-now を指定すると、実行時点(JST)以降に発走するレースだけを入れる。
AllRaceList.do から各場の発走時刻を取得してフィルタする。
"""
from __future__ import annotations
import argparse, re, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_jos(date: str) -> list[str]:
    r = requests.get(
        "https://www.oddspark.com/keirin/RaceListInfo.do",
        params={"kaisaiBi": date},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return sorted(set(re.findall(r"joCode=(\d+)", r.text)))


def fetch_race_post_times(jo: str, date: str) -> dict[int, str]:
    """AllRaceList.do から {race_no: 'HH:MM'} を返す。失敗時は空dict。"""
    try:
        r = requests.get(
            "https://www.oddspark.com/keirin/AllRaceList.do",
            params={"joCode": jo, "kaisaiBi": date},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  WARN jo={jo} AllRaceList取得失敗: {e}", file=sys.stderr)
        return {}
    # HTML実体参照・タグを落としてからパース（第NR … 発走時間 HH:MM）
    text = r.text
    text = text.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    pairs = re.findall(r"第(\d+)R[^第]*?発走時間\s*(\d{1,2}:\d{2})", text)
    out: dict[int, str] = {}
    for rn_s, hhmm in pairs:
        try:
            rn = int(rn_s)
        except ValueError:
            continue
        if 1 <= rn <= 12:
            out[rn] = hhmm
    return out


def hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = map(int, hhmm.split(":"))
    return hh * 60 + mm


def main():
    ap = argparse.ArgumentParser(description="開催日の joblist.txt を作成")
    ap.add_argument("--dates", required=True, help="YYYYMMDD をカンマ区切り")
    ap.add_argument("--out", default="joblist.txt")
    ap.add_argument(
        "--after-now",
        action="store_true",
        help="実行時点(JST)以降に発走するレースのみ joblist に入れる",
    )
    args = ap.parse_args()

    now = datetime.now(JST)
    now_min = now.hour * 60 + now.minute
    today_str = now.strftime("%Y%m%d")

    jobs: list[str] = []
    for d in [x.strip() for x in args.dates.split(",") if x.strip()]:
        jos = fetch_jos(d)
        print(d, jos, f"now={now.isoformat()}" if args.after_now else "")
        for jo in jos:
            if args.after_now:
                # 過去日は全レースが「今より前」なので対象なし
                if d < today_str:
                    print(f"  SKIP jo={jo}: {d} は過去日のため --after-now では対象なし")
                    continue
                times = fetch_race_post_times(jo, d)
                time.sleep(0.4)
                if not times:
                    # 時刻が取れない場合は従来どおり 1-12 を全部入れる（取りこぼし防止）
                    print(f"  WARN jo={jo}: 発走時刻取得失敗 → 1-12R を全て追加")
                    for rn in range(1, 13):
                        jobs.append(f"{d} {jo} {rn}")
                    continue
                kept = 0
                for rn in sorted(times.keys()):
                    post_min = hhmm_to_minutes(times[rn])
                    # 当日: 今以降の発走のみ。未来日: 全レース。
                    if d > today_str or post_min >= now_min:
                        jobs.append(f"{d} {jo} {rn}")
                        kept += 1
                    else:
                        print(f"  skip jo={jo} R{rn} 発走{times[rn]} (now以降ではない)")
                print(f"  jo={jo} kept={kept}/{len(times)} (after {now.strftime('%H:%M')})")
            else:
                for rn in range(1, 13):
                    jobs.append(f"{d} {jo} {rn}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(jobs) + ("\n" if jobs else ""))
    print(f"total_jobs={len(jobs)} -> {args.out}")
    if args.after_now and not jobs:
        print("WARN: --after-now の結果 joblist が空です（該当レースなし）", file=sys.stderr)


if __name__ == "__main__":
    main()
