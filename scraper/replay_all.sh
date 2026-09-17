#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"

echo "replay_all start"
AFTER=""
while true; do
  bash scraper/run_replay_continue.sh 30 3 1000000 "$AFTER" true || true
  AFTER=$(python3 -c "import os,requests; api=os.environ['API_BASE']; p={'since':'all','limit':30,'exclude_already_replayed':'false'}; a='$AFTER'.strip();
import sys
a=sys.argv[1]
p={'since':'all','limit':30,'exclude_already_replayed':'false'}
if a:
    p['after_race_id']=int(a)
d=requests.get(api+'/races/replay-settled/targets',params=p,timeout=90).json()
ids=d.get('race_ids') or []
print(ids[-1] if ids else '')" "$AFTER")
  echo "after=$AFTER"
  if [ -z "$AFTER" ]; then
    break
  fi
  sleep 10
done
echo "replay_all done"
