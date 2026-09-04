#!/usr/bin/env python3
"""確定済みレースの replay（race-plan → 再投票 → confirm）。

Render / Cloudflare の 429 対策:
- 一時的な 429/502/503 は指数バックオフでリトライ
- レース間に短い間隔を入れる
- Cloudflare チャレンジ HTML は JSON でないので検出して待機
"""
import argparse
import json
import os
import sys
import time

import requests


API_BASE = os.environ.get(
    "API_BASE",
    "https://keirin-ev-tool.onrender.com",
).rstrip("/")

RETRYABLE_STATUS = {429, 502, 503, 504}


def now():
    return time.strftime("%H:%M:%S")


def fmt_seconds(sec):
    if sec is None or sec < 0:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m:02d}m{s:02d}s"


def looks_like_cloudflare(text: str) -> bool:
    if not text:
        return False
    t = text[:500].lower()
    return (
        "just a moment" in t
        or "cf-browser-verification" in t
        or "challenge-platform" in t
        or "cloudflare" in t
    )


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int = 8,
    base_wait: float = 5.0,
    timeout: float = 180,
    **kwargs,
):
    """429/5xx/Cloudflare を指数バックオフでリトライ。成功時は Response を返す。"""
    last = None
    for attempt in range(max_retries + 1):
        try:
            r = session.request(method, url, timeout=timeout, **kwargs)
            last = r
            if r.status_code in RETRYABLE_STATUS or looks_like_cloudflare(r.text or ""):
                if attempt >= max_retries:
                    return r
                wait = min(base_wait * (2 ** attempt), 120.0)
                print(
                    f"[{now()}]   一時エラー status={r.status_code} "
                    f"→ {wait:.0f}s 待ってリトライ ({attempt + 1}/{max_retries})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            if attempt >= max_retries:
                raise
            wait = min(base_wait * (2 ** attempt), 120.0)
            print(
                f"[{now()}]   通信エラー {type(e).__name__}: {e} "
                f"→ {wait:.0f}s 待ってリトライ ({attempt + 1}/{max_retries})",
                flush=True,
            )
            time.sleep(wait)
    return last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="all")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--bankroll", type=float, default=1000000)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="レース間の待機秒（Render 429 緩和。既定 1.5）",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="詳細結果の JSONL パス。未指定時は scraper/data/replay_log_<時刻>.jsonl",
    )
    args = parser.parse_args()

    log_path = args.log_file
    if not log_path:
        os.makedirs("scraper/data", exist_ok=True)
        log_path = f"scraper/data/replay_log_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    log_f = open(log_path, "a", encoding="utf-8")
    print(f"[{now()}] 詳細ログ出力先: {log_path}", flush=True)
    print(f"[{now()}] レース間隔={args.interval}s / 429リトライ有効", flush=True)

    session = requests.Session()
    target_url = f"{API_BASE}/races/replay-settled/targets"
    warm_url = f"{API_BASE}/purchases/warm-calibration"

    print(f"[{now()}] 校正ウォームアップ: {warm_url}", flush=True)
    try:
        wr = request_with_retry(session, "POST", warm_url, timeout=300, max_retries=5)
        print(f"[{now()}] warm status={wr.status_code}", flush=True)
        print((wr.text or "")[:500], flush=True)
        if wr.status_code >= 400:
            print(f"[{now()}] 警告: warm失敗。続けますが不安定な可能性あり", flush=True)
    except Exception as e:
        print(f"[{now()}] warm error: {type(e).__name__}: {e}", flush=True)

    print(f"[{now()}] 対象レースID一覧取得: {target_url}", flush=True)
    try:
        r = request_with_retry(
            session,
            "GET",
            target_url,
            params={"since": args.since, "limit": args.limit},
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[{now()}] 対象ID取得エラー: {type(e).__name__}: {e}", flush=True)
        if "r" in locals():
            print(f"HTTP {getattr(r, 'status_code', '?')}", flush=True)
            print((getattr(r, "text", None) or "")[:2000], flush=True)
        sys.exit(1)

    race_ids = data.get("race_ids", [])
    print(f"[{now()}] 対象 {len(race_ids)} 件 (since={args.since})", flush=True)
    if not race_ids:
        print(f"[{now()}] 対象レースなし", flush=True)
        log_f.close()
        return

    total = len(race_ids)
    done = failed = skipped = 0
    started = time.time()

    for index, race_id in enumerate(race_ids, start=1):
        if index > 1 and args.interval > 0:
            time.sleep(args.interval)

        t0 = time.time()
        url = f"{API_BASE}/races/{race_id}/replay-settled"
        print(
            f"[{now()}] [{index}/{total} "
            f"{index / total * 100:6.2f}%] "
            f"race_id={race_id} 処理開始",
            flush=True,
        )

        try:
            r = request_with_retry(
                session,
                "POST",
                url,
                params={"bankroll": args.bankroll},
                timeout=180,
                max_retries=8,
                base_wait=8.0,
            )
            elapsed_one = time.time() - t0

            if 200 <= r.status_code < 300:
                try:
                    result = r.json()
                except Exception:
                    result = {"raw": (r.text or "")[:500]}

                stage = (result or {}).get("stage")
                if stage in ("skipped_no_odds", "skipped_no_win_probs"):
                    skipped += 1
                    print(
                        f"[{now()}] [{index}/{total}] SKIP ({elapsed_one:.1f}s) stage={stage}",
                        flush=True,
                    )
                else:
                    done += 1
                    timings = (result or {}).get("debug_timings") or {}
                    win_diag = (result or {}).get("winning_diagnostics") or []
                    status_counts = {}
                    for w in win_diag:
                        st = w.get("status") or "?"
                        status_counts[st] = status_counts.get(st, 0) + 1
                    print(
                        f"[{now()}] [{index}/{total}] 完了 ({elapsed_one:.1f}s) "
                        f"done={done} failed={failed} skipped={skipped}",
                        flush=True,
                    )
                    if timings:
                        # confirm など要点だけ
                        conf = timings.get("confirm_race_result")
                        plan = timings.get("race_plan_call_total")
                        print(
                            f"    timings: confirm={conf}s plan={plan}s "
                            f"plan_items={(result or {}).get('plan_items')} "
                            f"winning={status_counts}",
                            flush=True,
                        )
                    log_f.write(json.dumps({"race_id": race_id, "result": result}, ensure_ascii=False) + "\n")
                    log_f.flush()
            else:
                failed += 1
                print(
                    f"[{now()}] [{index}/{total}] HTTP ERROR "
                    f"status={r.status_code} ({elapsed_one:.1f}s)",
                    flush=True,
                )
                print((r.text or "")[:300], flush=True)
                log_f.write(
                    json.dumps(
                        {
                            "race_id": race_id,
                            "error": r.status_code,
                            "body": (r.text or "")[:500],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                log_f.flush()

        except Exception as e:
            failed += 1
            print(
                f"[{now()}] [{index}/{total}] ERROR: {type(e).__name__}: {e}",
                flush=True,
            )

        elapsed = time.time() - started
        processed = done + failed + skipped
        eta = (elapsed / processed * (total - processed)) if processed else None
        print(
            f"[{now()}] 進捗 {processed}/{total} "
            f"({processed / total * 100:6.2f}%) "
            f"done={done} failed={failed} skipped={skipped} "
            f"elapsed={fmt_seconds(elapsed)} ETA={fmt_seconds(eta)}",
            flush=True,
        )

    print("", flush=True)
    print(f"[{now()}] ===== replay 完了 =====", flush=True)
    print(f"total   = {total}", flush=True)
    print(f"done    = {done}", flush=True)
    print(f"failed  = {failed}", flush=True)
    print(f"skipped = {skipped}", flush=True)
    print(f"elapsed = {fmt_seconds(time.time() - started)}", flush=True)
    print(f"詳細ログ: {log_path}", flush=True)
    log_f.close()


if __name__ == "__main__":
    main()
