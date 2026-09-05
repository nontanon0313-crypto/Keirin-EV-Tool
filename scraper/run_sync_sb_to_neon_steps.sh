#!/data/data/com.termux/files/usr/bin/bash
# Supabase→Neon をステップ実行（502回避）
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
SEC="${ADMIN_SYNC_SECRET:-keirin-sync-nontanon0313}"

run_step() {
  local step="$1"
  echo "=== step=$step ==="
  python3 -c "
import requests, os, time
api='$API'
sec='$SEC'
step='$step'
for i in range(4):
    try:
        r=requests.post(api+'/admin/sync-supabase-to-neon', params={'token':sec,'step':step}, timeout=120)
        print('status', r.status_code)
        t=r.text or ''
        print(t[-2000:] if len(t)>2000 else t)
        if r.status_code==200:
            break
        if r.status_code in (502,503,504):
            time.sleep(5*(i+1))
            continue
        break
    except Exception as e:
        print('err', e)
        time.sleep(5*(i+1))
"
}

run_step banks
run_step races
run_step entries

# odds は残り0まで最大30回
for i in $(seq 1 30); do
  echo "=== odds round $i ==="
  out=$(python3 -c "
import requests
r=requests.post('$API/admin/sync-supabase-to-neon', params={'token':'$SEC','step':'odds'}, timeout=120)
print(r.status_code)
print(r.text[-1500:] if r.text else '')
")
  echo "$out"
  echo "$out" | grep -q '残りレース約0\|残り0\|要埋め0' && break
  echo "$out" | grep -q 'status 200' || sleep 3
done

run_step purchases
run_step skipped
run_step bankroll
echo "=== ALL STEPS DONE ==="
