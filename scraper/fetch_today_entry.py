#!/usr/bin/env python3
"""当日・全場の出走表のみ取得（発走時刻・締切付き）。オッズは取らない。"""
from __future__ import annotations
import argparse, json, os, re, sys, time, requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from keirin_oddspark_scraper import parse_entry, parse_sp_race_info, merge_sp_into_entry, get_soup, BASE

JST = ZoneInfo("Asia/Tokyo")

def parse_post_time(jo, kaisai_bi, race_no):
    soup = get_soup(f"{BASE}/RaceList.do", {"joCode": jo, "kaisaiBi": kaisai_bi, "raceNo": race_no})
    text = soup.get_text(" ", strip=True)
    post = close = None
    m = re.search(r"発走時間\s*(\d{1,2}:\d{2})", text)
    if m:
        post = m.group(1)
    m = re.search(r"締切予定\s*(\d{1,2}:\d{2})", text)
    if m:
        close = m.group(1)
    post_iso = close_iso = None
    y, mo, d = int(kaisai_bi[:4]), int(kaisai_bi[4:6]), int(kaisai_bi[6:8])
    if post:
        hh, mm = map(int, post.split(":"))
        post_iso = datetime(y, mo, d, hh, mm, tzinfo=JST).isoformat()
    if close:
        hh, mm = map(int, close.split(":"))
        close_iso = datetime(y, mo, d, hh, mm, tzinfo=JST).isoformat()
    return post, close, post_iso, close_iso

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD（省略時はJST今日）")
    ap.add_argument("--out", default=None, help="出力ディレクトリ")
    args = ap.parse_args()
    today = args.date or datetime.now(JST).strftime("%Y%m%d")
    out = Path(args.out or os.environ.get("KEIRIN_TODAY_DIR") or (HERE / "data" / "today_entry"))
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST)
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(
        "https://www.oddspark.com/keirin/RaceListInfo.do",
        params={"kaisaiBi": today},
        headers=headers,
        timeout=30,
    )
    jos = sorted(set(re.findall(r"joCode=(\d+)", r.text)))
    print("date", today, "venues", jos, "now", now.isoformat())
    ok = empty = fail = 0
    index = []
    for jo in jos:
        for rn in range(1, 13):
            path = out / f"{today}_{jo}_{rn:02d}_entry.json"
            try:
                entry = parse_entry(jo, today, rn)
                try:
                    sp = parse_sp_race_info(jo, today, rn)
                    entry = merge_sp_into_entry(entry, sp)
                except Exception:
                    entry.setdefault("lines", [])
                n = len(entry.get("riders", []))
                post = close = post_iso = close_iso = None
                if n > 0:
                    post, close, post_iso, close_iso = parse_post_time(jo, today, rn)
                entry["post_time"] = post
                entry["close_time"] = close
                entry["post_time_iso"] = post_iso
                entry["close_time_iso"] = close_iso
                mins = None
                if post_iso:
                    mins = int((datetime.fromisoformat(post_iso) - now).total_seconds() // 60)
                data = {
                    "jo_code": jo,
                    "kaisai_bi": today,
                    "race_no": rn,
                    "scraped_at": datetime.now(JST).isoformat(),
                    "entry_only": True,
                    "entry": entry,
                    "mins_to_post": mins,
                    "odds": {},
                    "result": {},
                    "data_quality": {"entry_ok": n > 0, "result_ok": False, "all_complete": False},
                }
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if n > 0:
                    print(f"OK  jo={jo} R{rn:02d} riders={n} 発走={post} あと{mins}分")
                    ok += 1
                    index.append({
                        "file": path.name, "jo": jo, "race": rn, "riders": n,
                        "race_name": entry.get("race_name"),
                        "post_time": post, "close_time": close,
                        "post_time_iso": post_iso, "mins_to_post": mins,
                    })
                else:
                    empty += 1
                time.sleep(0.6)
            except Exception as e:
                print(f"FAIL jo={jo} R{rn} {type(e).__name__}: {e}")
                fail += 1
                time.sleep(1.0)
    upcoming = [x for x in index if x.get("mins_to_post") is not None and 0 <= x["mins_to_post"] <= 30]
    upcoming.sort(key=lambda x: x["mins_to_post"])
    summary = {
        "date": today, "now": now.isoformat(), "venues": jos,
        "ok": ok, "empty": empty, "fail": fail,
        "upcoming_within_30min": upcoming,
        "races_with_entry": sorted(index, key=lambda x: (x.get("mins_to_post") is None, x.get("mins_to_post") or 9999)),
    }
    (out / f"index_{today}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ok={ok} empty={empty} fail={fail} upcoming30={len(upcoming)}")
    print("index:", out / f"index_{today}.json")

if __name__ == "__main__":
    main()
