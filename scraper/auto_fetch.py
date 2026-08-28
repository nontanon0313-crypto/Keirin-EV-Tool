#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
joblist.txt の全ジョブを順処理。取得済み(JSONの構造が正常)はスキップ。
例外・タイムアウトでも次へ進み、未完了なら周回再開。

環境変数:
  KEIRIN_DATA_DIR     出力先（既定: ./data）
  KEIRIN_SCRAPER_DIR  scraper ディレクトリ（既定: このファイルの場所）
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("KEIRIN_DATA_DIR", HERE / "data"))
SCRAPER = Path(os.environ.get("KEIRIN_SCRAPER_DIR", HERE))
OUT.mkdir(parents=True, exist_ok=True)
JOBLIST = OUT / "joblist.txt"
LOG = OUT / "fetch.log"
WATCH = OUT / "watchdog.log"
STATUS = OUT / "status.json"
RACE_TIMEOUT = 300
SLEEP_BETWEEN = 1
SLEEP_RESTART = 5

def wlog(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(WATCH, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def flog(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def load_jobs():
    if not JOBLIST.is_file():
        raise FileNotFoundError(f"joblist.txt not found: {JOBLIST}")
    jobs = []
    for line in JOBLIST.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3:
            jobs.append((parts[0], parts[1], int(parts[2])))
    return jobs

def json_path(day: str, jo: str, rn: int) -> Path:
    return OUT / f"{day}_{jo}_{rn:02d}.json"

def is_done(day: str, jo: str, rn: int) -> bool:
    """
    完了判定をファイルサイズ(旧: MIN_SIZE超か)ではなく、JSONの中身の構造で行う。

    のんの実機運用で判明したバグ: そのjo/rnにレース自体が無い(riders=0)場合、
    JSONは正常に書けているのにファイルサイズが2000バイトを下回り、
    「未完了」と誤判定されて同じジョブを無限に再取得し続けていた。
    レースが無いこと自体は正常な結果であり、失敗ではない。
    ここでは「JSONとして読め、scraped_atがあり、entry.ridersがlist型である」
    ことだけを完了の条件にする(riders=[]でも構わない=それが正しい結果)。
    """
    p = json_path(day, jo, rn)
    if not p.is_file():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False  # 壊れたJSON(書き込み途中でのクラッシュ等)は未完了扱いにして取り直す
    if "scraped_at" not in d:
        return False
    riders = (d.get("entry") or {}).get("riders")
    return isinstance(riders, list)

def progress(jobs):
    pending = [(d, j, r) for d, j, r in jobs if not is_done(d, j, r)]
    info = {
        "total": len(jobs),
        "done": len(jobs) - len(pending),
        "pending": len(pending),
        "next": f"{pending[0][0]} jo={pending[0][1]} R{pending[0][2]}" if pending else None,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    STATUS.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info

def run_one(day: str, jo: str, rn: int) -> int:
    flog(f"START {day} jo={jo} R{rn}")
    env = {**os.environ, "KEIRIN_DATA_DIR": str(OUT)}
    try:
        r = subprocess.run(
            [sys.executable, "-u", "run_keirin.py", "--date", day, "--jo", jo, "--race", str(rn)],
            cwd=str(SCRAPER), env=env, capture_output=True, text=True, timeout=RACE_TIMEOUT,
        )
        if r.stdout:
            flog(r.stdout.rstrip()[-2000:])
        if r.stderr:
            flog(r.stderr.rstrip()[-500:])
        flog(f"END {day} jo={jo} R{rn} code={r.returncode}")
        return r.returncode
    except subprocess.TimeoutExpired:
        flog(f"ERR {day} jo={jo} R{rn} TimeoutExpired")
        return -1
    except Exception as e:
        flog(f"ERR {day} jo={jo} R{rn} {type(e).__name__}: {e}")
        return -1

def main() -> int:
    if not (SCRAPER / "run_keirin.py").is_file():
        wlog(f"ERROR: run_keirin.py not found in {SCRAPER}")
        return 1
    jobs = load_jobs()
    wlog(f"auto_fetch start out={OUT} scraper={SCRAPER} jobs={len(jobs)}")
    while True:
        info = progress(jobs)
        wlog(f"progress done={info['done']}/{info['total']} pending={info['pending']} next={info['next']}")
        if info["pending"] == 0:
            wlog("ALL JOBS DONE")
            flog("BATCH_DONE")
            return 0
        for i, (day, jo, rn) in enumerate(jobs, 1):
            if is_done(day, jo, rn):
                continue
            code = run_one(day, jo, rn)
            # 以前はこの1件ごとの進捗が画面に出ず、数時間かかる取得の間ずっと
            # 「本当に動いているのか」が分からなかった(のんの実機運用で判明・修正)。
            info = progress(jobs)
            wlog(f"取得 {day} jo={jo} R{rn} (code={code}) → 進捗 {info['done']}/{info['total']}件完了")
            time.sleep(SLEEP_BETWEEN)
        info = progress(jobs)
        if info["pending"] == 0:
            wlog("ALL JOBS DONE")
            flog("BATCH_DONE")
            return 0
        wlog(f"pass finished pending={info['pending']} → sleep {SLEEP_RESTART}s")
        time.sleep(SLEEP_RESTART)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        wlog("interrupted")
        raise SystemExit(130)
