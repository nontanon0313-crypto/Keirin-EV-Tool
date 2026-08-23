#!/usr/bin/env python3
"""開催日の全場×1-12R の joblist.txt を作成する。"""
from __future__ import annotations
import argparse, os, re, requests

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True, help="YYYYMMDD をカンマ区切り")
    ap.add_argument("--out", default="joblist.txt")
    args = ap.parse_args()
    headers = {"User-Agent": "Mozilla/5.0"}
    jobs = []
    for d in [x.strip() for x in args.dates.split(",") if x.strip()]:
        r = requests.get(
            "https://www.oddspark.com/keirin/RaceListInfo.do",
            params={"kaisaiBi": d},
            headers=headers,
            timeout=30,
        )
        jos = sorted(set(re.findall(r"joCode=(\d+)", r.text)))
        print(d, jos)
        for jo in jos:
            for rn in range(1, 13):
                jobs.append(f"{d} {jo} {rn}")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(jobs) + "\n")
    print(f"total_jobs={len(jobs)} -> {args.out}")

if __name__ == "__main__":
    main()
