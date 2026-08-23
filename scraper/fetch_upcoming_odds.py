#!/usr/bin/env python3
"""index / entry JSON から「N分以内に発走」のレースだけオッズ付きで取得。"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from keirin_oddspark_scraper import scrape_one_race

JST = ZoneInfo("Asia/Tokyo")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYYMMDD")
    ap.add_argument("--within-min", type=int, default=30)
    ap.add_argument("--entry-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    today = args.date or datetime.now(JST).strftime("%Y%m%d")
    entry_dir = Path(args.entry_dir or os.environ.get("KEIRIN_TODAY_DIR") or (HERE / "data" / "today_entry"))
    out_dir = Path(args.out or os.environ.get("KEIRIN_UPCOMING_DIR") or (HERE / "data" / "upcoming_odds"))
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST)
    within = args.within_min
    index_path = entry_dir / f"index_{today}.json"
    targets = []
    if index_path.is_file():
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        for r in idx.get("races_with_entry", []):
            iso = r.get("post_time_iso")
            if not iso:
                continue
            mins = int((datetime.fromisoformat(iso) - now).total_seconds() // 60)
            if 0 <= mins <= within:
                targets.append({"jo": str(r["jo"]), "race": int(r["race"]),
                                "post_time": r.get("post_time"), "mins_to_post": mins})
    else:
        for p in sorted(entry_dir.glob(f"{today}_*_entry.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            iso = (d.get("entry") or {}).get("post_time_iso")
            if not iso:
                continue
            mins = int((datetime.fromisoformat(iso) - now).total_seconds() // 60)
            if 0 <= mins <= within and len((d.get("entry") or {}).get("riders") or []) > 0:
                targets.append({"jo": str(d["jo_code"]), "race": int(d["race_no"]),
                                "post_time": (d.get("entry") or {}).get("post_time"),
                                "mins_to_post": mins})
    targets.sort(key=lambda x: x["mins_to_post"])
    print(f"now={now.isoformat()} within={within} targets={len(targets)}")
    for t in targets:
        print(f"  あと{t['mins_to_post']}分 jo={t['jo']} R{t['race']} 発走{t['post_time']}")
    if not targets:
        print("対象なし")
        return 0
    ok = fail = 0
    results = []
    for t in targets:
        jo, rn = t["jo"], t["race"]
        out = out_dir / f"{today}_{jo}_{rn:02d}.json"
        print(f">>> jo={jo} R{rn}")
        t0 = time.time()
        try:
            data = scrape_one_race(jo, today, rn)
            ep = entry_dir / f"{today}_{jo}_{rn:02d}_entry.json"
            if ep.is_file():
                ed = json.loads(ep.read_text(encoding="utf-8"))
                for k in ("post_time", "close_time", "post_time_iso", "close_time_iso"):
                    if (ed.get("entry") or {}).get(k):
                        data.setdefault("entry", {})[k] = ed["entry"][k]
            data["mins_to_post"] = t["mins_to_post"]
            data["upcoming_fetch"] = True
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"OK {time.time()-t0:.0f}s -> {out.name}")
            ok += 1
            results.append({"jo": jo, "race": rn, "mins": t["mins_to_post"], "file": out.name, "ok": True})
        except Exception as e:
            print(f"FAIL {type(e).__name__}: {e}")
            fail += 1
            results.append({"jo": jo, "race": rn, "ok": False, "error": str(e)})
        time.sleep(1.0)
    summary = {"date": today, "now": now.isoformat(), "within_min": within, "ok": ok, "fail": fail, "results": results}
    (out_dir / f"upcoming_{today}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ok={ok} fail={fail}")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
