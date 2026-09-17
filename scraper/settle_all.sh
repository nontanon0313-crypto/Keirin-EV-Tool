#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"
export KEIRIN_API_BASE="$API_BASE"
DATA="${KEIRIN_DATA_DIR:-$PWD/scraper/data}"
mkdir -p "$DATA"

TODAY=$(python3 -c "from datetime import datetime, timezone, timedelta; print(datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d'))")
echo "1) today settle $TODAY"
bash scraper/run_day.sh "$TODAY"

echo "2) confirm_pending from data"
python3 scraper/confirm_pending.py --dir "$DATA"

echo "3) check unsettled today"
python3 -c "import os,requests; api=os.environ['API_BASE']; t=requests.get(api+'/races/today',timeout=60).json(); print('unsettled_today', len(t) if isinstance(t,list) else t)"
echo "settle_all done"
