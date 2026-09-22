#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "python3 not found" >&2; exit 1; fi

DATE="${1:-$($PY -c 'from datetime import datetime;from zoneinfo import ZoneInfo;print(datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d"))')}"
if ! [[ "$DATE" =~ ^[0-9]{8}$ ]]; then
  echo "DATE must be YYYYMMDD: $DATE" >&2; exit 1
fi

export KEIRIN_DATA_DIR="${KEIRIN_DATA_DIR:-$PWD/data}"
mkdir -p "$KEIRIN_DATA_DIR"

TODAY="$($PY -c 'from datetime import datetime;from zoneinfo import ZoneInfo;print(datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d"))')"
ARGS=(--dates "$DATE" --out "$KEIRIN_DATA_DIR/joblist.txt")
if [ "$DATE" = "$TODAY" ]; then
  ARGS+=(--after-now)
  echo "=== $DATE incomplete (after now) data=$KEIRIN_DATA_DIR ==="
else
  echo "=== $DATE full day data=$KEIRIN_DATA_DIR ==="
fi

echo "--- 1/4 joblist ---"
$PY make_joblist.py "${ARGS[@]}"

COUNT="$($PY -c "from pathlib import Path;import os;p=Path(os.environ.get('KEIRIN_DATA_DIR','data'))/'joblist.txt';print(sum(1 for L in (p.read_text(encoding='utf-8').splitlines() if p.is_file() else []) if len(L.split())==3))")"
if [ "$COUNT" -eq 0 ]; then
  echo "no races; exit"; exit 0
fi

echo "--- 2/4 fetch ($COUNT jobs) ---"
$PY -u auto_fetch.py

echo "--- 3/4 pipeline ---"
set +e
$PY run_full_pipeline.py --dir "$KEIRIN_DATA_DIR" --date "$DATE" --concurrency 1 --skip-plan
EC=$?
set -e
if [ "$EC" -eq 2 ]; then echo "Gemini quota; re-run later"; exit 2; fi
if [ "$EC" -ne 0 ]; then exit "$EC"; fi

echo "--- 4/4 repair ---"
$PY -c "import os,sys
try:
 import requests
except ImportError:
 sys.exit(0)
b=os.environ.get('API_BASE','https://keirin-ev-tool.onrender.com').rstrip('/')
try:
 r=requests.post(b+'/races/repair-broken-results',params={'apply':True},timeout=90)
 if r.status_code==404: print('repair skip'); sys.exit(0)
 r.raise_for_status(); d=r.json(); print('broken',d.get('broken_count'),'applied',d.get('applied'))
except Exception as e:
 print('repair skip',e)" || true

echo "=== done $DATE ==="
