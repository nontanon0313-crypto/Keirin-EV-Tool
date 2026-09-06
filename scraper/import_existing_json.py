import json
import time
from pathlib import Path

import requests

API_BASE = "https://keirin-ev-tool.onrender.com"
DATA_DIR = Path("scraper/data")
TIMEOUT = 90
MAX_RETRIES = 3

files = sorted(DATA_DIR.glob("*.json"))

print(f"JSON_COUNT: {len(files)}")

if not files:
    raise SystemExit("JSONファイルがありません")

session = requests.Session()

success = 0
skipped = 0
failed = []

for index, path in enumerate(files, 1):
    try:
        payload = json.loads(path.read_text())

        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = session.post(
                    f"{API_BASE}/scraper-import/race",
                    json=payload,
                    timeout=TIMEOUT,
                )

                if response.status_code == 200:
                    result = response.json()

                    if result.get("skipped"):
                        skipped += 1
                    else:
                        success += 1

                    print(
                        f"[{index}/{len(files)}] OK "
                        f"{path.name} "
                        f"race_id={result.get('race_id')} "
                        f"skipped={result.get('skipped', False)}"
                    )
                    break

                last_error = (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

            except Exception as e:
                last_error = repr(e)

            if attempt < MAX_RETRIES:
                print(
                    f"[{index}/{len(files)}] RETRY "
                    f"{path.name} "
                    f"attempt={attempt}/{MAX_RETRIES} "
                    f"error={last_error}"
                )
                time.sleep(5 * attempt)
        else:
            failed.append({
                "file": str(path),
                "error": last_error,
            })
            print(
                f"[{index}/{len(files)}] FAILED "
                f"{path.name}: {last_error}"
            )

    except Exception as e:
        failed.append({
            "file": str(path),
            "error": repr(e),
        })
        print(
            f"[{index}/{len(files)}] FILE_ERROR "
            f"{path.name}: {e!r}"
        )

    if index % 25 == 0:
        print(
            f"=== PROGRESS {index}/{len(files)} "
            f"success={success} "
            f"skipped={skipped} "
            f"failed={len(failed)} ==="
        )

    # Render/DBへの連続負荷を少し抑える
    time.sleep(0.15)

Path("scraper/data/import_failures.json").write_text(
    json.dumps(
        failed,
        ensure_ascii=False,
        indent=2,
    )
)

print()
print("=== IMPORT FINISHED ===")
print(f"TOTAL: {len(files)}")
print(f"SUCCESS: {success}")
print(f"SKIPPED: {skipped}")
print(f"FAILED: {len(failed)}")
print("FAILURE_FILE: scraper/data/import_failures.json")
