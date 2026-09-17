#!/data/data/com.termux/files/usr/bin/bash
# 未確定レースの着順確定（当日＋過去・ローカルJSONにある分）
# 使い方: bash scraper/settle_all.sh
set -euo pipefail
ROOT="\( (cd " \)(dirname "$0")/.." && pwd)"
cd "$ROOT"
export API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"
export KEIRIN_API_BASE="$API_BASE"
DATA="${KEIRIN_DATA_DIR:-$ROOT/scraper/data}"
mkdir -p "$DATA"

TODAY="$(python3 -c 'from datetime import datetime, timezone, timedelta; jst=timezone(timedelta(hours=9)); print(datetime.now(jst).strftime("%Y%m%d"))')"
echo "=== 1) 今日分の取得〜結果確定: $TODAY ==="
bash "$ROOT/scraper/run_day.sh" "$TODAY"

echo "=== 2) data配下JSONの着順確定（過去日含む） ==="
python3 "$ROOT/scraper/confirm_pending.py" --dir "$DATA"

echo "=== 3) 未確定の残数確認 ==="
python3 - <<'PY'
import os, requests
api=os.environ["API_BASE"]
today=requests.get(api+"/races/today", timeout=60).json()
print("today未確定(着順なし)", len(today) if isinstance(today, list) else today)
try:
    all_today=requests.get(api+"/races/today-all", timeout=60).json()
    no=[r for r in all_today if not r.get("actual_result")]
    print("today-all 着順なし", len(no), "/", len(all_today))
except Exception as e:
    print("today-all", e)
PY
echo "settle_all 完了。着順なしが残る日は bash scraper/run_day.sh YYYYMMDD を追加実行"
