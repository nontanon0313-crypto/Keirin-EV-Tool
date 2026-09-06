#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path

OUT = Path(os.environ.get("KEIRIN_DATA_DIR", Path.home() / "keirin_fetch/data"))
SCRAPER = Path(os.environ.get("KEIRIN_SCRAPER_DIR", Path.home() / "keirin_fetch/Keirin-EV-Tool/scraper"))
OUT.mkdir(parents=True, exist_ok=True)
JOBLIST = OUT / "joblist.txt"
LOG = OUT / "fetch.log"
WATCH = OUT / "watchdog.log"
STATUS = OUT / "status.json"
MIN_SIZE = 2000
RACE_TIMEOUT = 300

def wlog(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(WATCH, "a") as f:
        f.write(line + "\n")

def flog(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")

def jobs():
    return [tuple(l.split()) for l in JOBLIST.read_text().splitlines() if len(l.split()) == 3]

def path(day, jo, rn):
    return OUT / f"{day}_{jo}_{int(rn):02d}.json"

def done(day, jo, rn):
    p = path(day, jo, rn)
    return p.is_file() and p.stat().st_size > MIN_SIZE

def progress(js):
    pending = [(d, j, r) for d, j, r in js if not done(d, j, int(r))]
    info = {
        "total": len(js),
        "done": len(js) - len(pending),
        "pending": len(pending),
        "next": f"{pending[0][0]} jo={pending[0][1]} R{pending[0][2]}" if pending else None,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    STATUS.write_text(json.dumps(info, ensure_ascii=False, indent=2))
    return info

def run_one(day, jo, rn):
    flog(f"START {day} jo={jo} R{rn}")
    env = {**os.environ, "KEIRIN_DATA_DIR": str(OUT)}
    try:
        r = subprocess.run(
            [sys.executable, "-u", "run_keirin.py", "--date", day, "--jo", jo, "--race", str(rn)],
            cwd=str(SCRAPER), env=env, capture_output=True, text=True, timeout=RACE_TIMEOUT,
        )
        if r.stdout:
            flog(r.stdout[-1500:])
        if r.stderr:
            flog(r.stderr[-500:])
        flog(f"END {day} jo={jo} R{rn} code={r.returncode}")
    except Exception as e:
        flog(f"ERR {day} jo={jo} R{rn} {type(e).__name__}: {e}")

def main():
    if not (SCRAPER / "run_keirin.py").is_file():
        wlog(f"ERROR: scraper not found: {SCRAPER}")
        return 1
    js = jobs()
    wlog(f"start jobs={len(js)} out={OUT}")
    while True:
        info = progress(js)
        wlog(f"progress done={info['done']}/{info['total']} next={info['next']}")
        if info["pending"] == 0:
            wlog("ALL DONE")
            flog("BATCH_DONE")
            return 0
        for day, jo, rn in js:
            if done(day, jo, int(rn)):
                continue
            run_one(day, jo, int(rn))
            time.sleep(1)
            progress(js)
        time.sleep(5)

if __name__ == "__main__":
    raise SystemExit(main())
