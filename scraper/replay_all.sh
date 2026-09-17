#!/data/data/com.termux/files/usr/bin/bash
# 確定済み全レースを現行ロジックで再投票
# 使い方: bash scraper/replay_all.sh
# 前提: 未確定の着順が無いこと（先に settle_all.sh）
set -euo pipefail
ROOT="\( (cd " \)(dirname "$0")/.." && pwd)"
cd "$ROOT"
export API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"

echo "=== 再投票開始（全ID） ==="
AFTER=""
while true; do
  bash "$ROOT/scraper/run_replay_continue.sh" 30 3 1000000 "$AFTER" true || true
  AFTER="$(python3 -c "
import os, requests
api=os.environ['API_BASE']
params={'since':'all','limit':30,'exclude_already_replayed':'false'}
after='$AFTER'.strip()
if after:
    params['after_race_id']=int(after)
d=requests.get(api+'/races/replay-settled/targets', params=params, timeout=90).json()
ids=d.get('race_ids') or []
print(ids[-1] if ids else '')
")"
  echo "after=$AFTER"
  [ -z "$AFTER" ] && break
  sleep 10
done
echo "replay_all 完了"
