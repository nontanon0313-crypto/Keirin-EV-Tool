#!/data/data/com.termux/files/usr/bin/bash
# Supabase→Neon ステップ同期（90秒以内・再開可能）
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
SEC="${ADMIN_SYNC_SECRET:-keirin-sync-nontanon0313}"

run_step() {
  local step="$1"
  local after="${2:-0}"
  echo "=== step=$step after_id=$after ==="
  python3 -c "
import requests, time, re
api='$API'; sec='$SEC'; step='$step'; after=$after
for i in range(5):
    try:
        r=requests.post(api+'/admin/sync-supabase-to-neon',
            params={'token':sec,'step':step,'after_id':after}, timeout=130)
        print('status', r.status_code)
        t=r.text or ''
        print(t[-2500:] if len(t)>2500 else t)
        if r.status_code==200:
            break
        if r.status_code in (502,503,504):
            time.sleep(4*(i+1)); continue
        break
    except Exception as e:
        print('err', e); time.sleep(4*(i+1))
"
}

# 続き専用: purchases / skipped を last_id 進めながら完了まで
# 第2引数で開始after_idを指定すれば途中から再開できる(同期済み分はスキップ)
run_chunked() {
  local step="$1"
  local after="${2:-0}"
  for i in $(seq 1 400); do
    echo "=== $step round $i after_id=$after ==="
    out=$(python3 -c "
import requests, re
r=requests.post('$API/admin/sync-supabase-to-neon',
  params={'token':'$SEC','step':'$step','after_id':$after}, timeout=130)
print('status', r.status_code)
print(r.text[-2000:] if r.text else '')
")
    echo "$out"
    # last_id 抽出
    lid=$(echo "$out" | sed -n 's/.*last_id=\([0-9][0-9]*\).*/\1/p' | tail -1)
    if echo "$out" | grep -q '残り0\|この帯は完了'; then
      echo "$step 完了"
      break
    fi
    if echo "$out" | grep -q 'status 200' && [ -n "$lid" ]; then
      after=$lid
    else
      sleep 3
    fi
  done
}

START_FROM="${1:-all}"

if [ "$START_FROM" = "skipped" ]; then
  run_chunked skipped "${2:-0}"
  run_step bankroll
  echo "=== DONE from skipped ==="
  exit 0
fi
if [ "$START_FROM" = "purchases" ]; then
  run_chunked purchases "${2:-0}"
  run_chunked skipped 0
  run_step bankroll
  echo "=== DONE from purchases ==="
  exit 0
fi

run_step banks
run_step races
run_step entries

for i in $(seq 1 30); do
  echo "=== odds round $i ==="
  out=$(python3 -c "
import requests
r=requests.post('$API/admin/sync-supabase-to-neon', params={'token':'$SEC','step':'odds'}, timeout=130)
print('status', r.status_code)
print(r.text[-1500:] if r.text else '')
")
  echo "$out"
  echo "$out" | grep -q '残りレース約0\|要埋め0\|残り0' && break
  sleep 1
done

run_chunked purchases
run_chunked skipped
run_step bankroll
echo "=== ALL STEPS DONE ==="
