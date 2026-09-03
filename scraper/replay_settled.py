#!/usr/bin/env python3
import argparse
import os
import sys
import time

import requests


API_BASE = os.environ.get(
    "API_BASE",
    "https://keirin-ev-tool.onrender.com",
).rstrip("/")


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="all")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--bankroll", type=float, default=1000000)
    args = parser.parse_args()

    session = requests.Session()

    # ここで取得するのは「対象レースID一覧」のみ。
    # 外部サイトからレース情報を取得する処理ではない。
    target_url = f"{API_BASE}/races/replay-settled/targets"

    # 校正キャッシュを先に温める（初回race-planの150sタイムアウト回避）
    warm_url = f"{API_BASE}/purchases/warm-calibration"
    print(f"[{now()}] 校正ウォームアップ: {warm_url}", flush=True)
    try:
        wr = session.post(warm_url, timeout=300)
        print(f"[{now()}] warm status={wr.status_code}", flush=True)
        print((wr.text or "")[:500], flush=True)
        if wr.status_code >= 400:
            print(f"[{now()}] 警告: warm失敗。続けますが初回が遅延/500になる可能性あり", flush=True)
    except Exception as e:
        print(f"[{now()}] warm error: {type(e).__name__}: {e}", flush=True)
        print(f"[{now()}] 警告: warm失敗のまま続行", flush=True)

    print(f"[{now()}] 対象レースID一覧取得: {target_url}", flush=True)

    try:
        r = session.get(
            target_url,
            params={
                "since": args.since,
                "limit": args.limit,
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[{now()}] 対象ID取得エラー: {type(e).__name__}: {e}", flush=True)
        if "r" in locals():
            print(f"HTTP {r.status_code}", flush=True)
            print((r.text or "")[:2000], flush=True)
        sys.exit(1)

    race_ids = data.get("race_ids", [])

    print(
        f"[{now()}] 対象 {len(race_ids)} 件 "
        f"(since={args.since})",
        flush=True,
    )

    if not race_ids:
        print(f"[{now()}] 対象レースなし", flush=True)
        return

    total = len(race_ids)
    done = 0
    failed = 0
    skipped = 0
    started = time.time()

    for index, race_id in enumerate(race_ids, start=1):
        t0 = time.time()
        url = f"{API_BASE}/races/{race_id}/replay-settled"

        print(
            f"[{now()}] [{index}/{total} "
            f"{index / total * 100:6.2f}%] "
            f"race_id={race_id} 処理開始",
            flush=True,
        )

        try:
            r = session.post(
                url,
                params={"bankroll": args.bankroll},
                timeout=180,
            )

            elapsed_one = time.time() - t0

            if 200 <= r.status_code < 300:
                done += 1

                try:
                    result = r.json()
                except Exception:
                    result = {}

                print(
                    f"[{now()}] [{index}/{total}] "
                    f"完了 "
                    f"({elapsed_one:.1f}s) "
                    f"done={done} failed={failed} skipped={skipped}",
                    flush=True,
                )

                if result:
                    print(
                        f"    result={result}",
                        flush=True,
                    )

            else:
                failed += 1

                print(
                    f"[{now()}] [{index}/{total}] "
                    f"HTTP ERROR "
                    f"status={r.status_code} "
                    f"({elapsed_one:.1f}s)",
                    flush=True,
                )

                print(
                    "    response:",
                    flush=True,
                )
                print(
                    (r.text or "")[:2000],
                    flush=True,
                )

        except requests.RequestException as e:
            failed += 1

            print(
                f"[{now()}] [{index}/{total}] "
                f"REQUEST ERROR: {type(e).__name__}: {e}",
                flush=True,
            )

            response = getattr(e, "response", None)
            if response is not None:
                print(
                    f"    HTTP status={response.status_code}",
                    flush=True,
                )
                print(
                    (response.text or "")[:2000],
                    flush=True,
                )

        except Exception as e:
            failed += 1

            print(
                f"[{now()}] [{index}/{total}] "
                f"UNEXPECTED ERROR: {type(e).__name__}: {e}",
                flush=True,
            )

        elapsed = time.time() - started
        processed = done + failed + skipped

        if processed > 0:
            avg = elapsed / processed
            remain = total - processed
            eta = avg * remain
        else:
            eta = None

        print(
            f"[{now()}] 進捗 "
            f"{processed}/{total} "
            f"({processed / total * 100:6.2f}%) "
            f"done={done} "
            f"failed={failed} "
            f"skipped={skipped} "
            f"elapsed={fmt_seconds(elapsed)} "
            f"ETA={fmt_seconds(eta)}",
            flush=True,
        )

    elapsed = time.time() - started

    print("", flush=True)
    print(f"[{now()}] ===== replay 完了 =====", flush=True)
    print(f"total   = {total}", flush=True)
    print(f"done    = {done}", flush=True)
    print(f"failed  = {failed}", flush=True)
    print(f"skipped = {skipped}", flush=True)
    print(f"elapsed = {fmt_seconds(elapsed)}", flush=True)


if __name__ == "__main__":
    main()
