#!/usr/bin/env python3
# いま active のDB上の「確定済み」を現行ロジックで再投票（集計用）
# 使い方: python3 scraper/replay_active_db.py
from __future__ import annotations
import json, os, sys, time
from pathlib import Path
import requests

API = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")
BANKROLL = float(os.environ.get("KEIRIN_PLAN_BANKROLL", "1000000"))
INTERVAL = float(os.environ.get("KEIRIN_PLAN_INTERVAL", "2.0"))
# false=既にプラン済みも消して再投票 / true=未プランだけ
EXCLUDE_DONE = os.environ.get("KEIRIN_EXCLUDE_REPLAYED", "false").lower() in ("1", "true", "yes")
DATA = Path(os.environ.get("KEIRIN_DATA_DIR", Path(__file__).resolve().parent / "data"))
PROGRESS = DATA / "replay_active_db_progress.json"
SESSION = requests.Session()

def log(msg):
    print(msg, flush=True)

def load():
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "after_race_id": None}

def save(prog):
    DATA.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")

def req(method, url, max_retries=8, timeout=180, **kw):
    last = None
    for attempt in range(max_retries + 1):
        try:
            r = SESSION.request(method, url, timeout=timeout, **kw)
            last = r
            if r.status_code in (429, 502, 503, 504):
                if attempt >= max_retries:
                    return r
                wait = min(5.0 * (2 ** attempt), 120.0)
                log(f"  HTTP {r.status_code} → {wait:.0f}s ({attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            if attempt >= max_retries:
                log(f"  通信失敗: {type(e).__name__}: {e}")
                return last
            wait = min(5.0 * (2 ** attempt), 120.0)
            log(f"  ERR {type(e).__name__}: {e} → {wait:.0f}s")
            time.sleep(wait)
    return last

def main():
    log(f"API={API} BANKROLL={BANKROLL:,.0f} exclude_already_replayed={EXCLUDE_DONE}")
    h = req("GET", f"{API}/health", timeout=60)
    if h is None or h.status_code != 200:
        log(f"health FAIL status={getattr(h, 'status_code', None)}")
        return 1
    db = (h.json() or {}).get("database") or {}
    log(f"active={db.get('active')} primary_ok={db.get('primary_ok')} fallback_ok={db.get('fallback_ok')} fallback2_ok={db.get('fallback2_ok')}")
    log(f"→ 再投票の書き込み先は active={db.get('active')} のみ（他DBは触らない）")

    prog = load()
    params = {
        "since": "all",
        "limit": 5000,
        "exclude_already_replayed": "true" if EXCLUDE_DONE else "false",
    }
    if prog.get("after_race_id") is not None:
        params["after_race_id"] = prog["after_race_id"]

    log(f"targets {params}")
    tr = req("GET", f"{API}/races/replay-settled/targets", params=params, timeout=120)
    if tr is None or tr.status_code >= 400:
        log(f"targets FAIL status={getattr(tr, 'status_code', None)}")
        return 1
    data = tr.json()
    ids = data.get("race_ids") or []
    log(f"total={data.get('total')} returned={len(ids)}")

    for rid in ids:
        key = str(rid)
        if prog["done"].get(key) == "ok":
            continue
        log(f"replay-settled race_id={rid}")
        r = req("POST", f"{API}/races/{rid}/replay-settled", json={"bankroll": BANKROLL}, timeout=300)
        if r is None or r.status_code >= 400:
            log(f"  FAIL {rid} status={getattr(r, 'status_code', None)}")
            prog["done"][key] = "fail"
        else:
            j = r.json() if r.content else {}
            log(f"  OK {rid} stage={j.get('stage')} plan_items={j.get('plan_items')} purchases={j.get('purchases_recorded')}")
            prog["done"][key] = "ok"
            prog["after_race_id"] = rid
        save(prog)
        time.sleep(INTERVAL)

    log("=== DONE（この active DB 分）===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
