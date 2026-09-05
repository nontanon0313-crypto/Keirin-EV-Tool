#!/data/data/com.termux/files/usr/bin/bash
# DB統合(双方向差分同期) → warm → 全体再投票 → 直近PVA検証
# 使い方:
#   bash scraper/run_merge_and_full_replay.sh
#   bash scraper/run_merge_and_full_replay.sh 200 2    # まず200件だけ試す
# 環境変数:
#   ADMIN_SYNC_SECRET  (必須・同期時)  未設定なら同期スキップしてreplayのみ
#   API_BASE           既定 https://keirin-ev-tool.onrender.com
#   SKIP_SYNC=1        同期を飛ばす
#   SKIP_REPLAY=1      再投票を飛ばす
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
SECRET="${ADMIN_SYNC_SECRET:-}"
LIMIT="${1:-5000}"
INTERVAL="${2:-2}"
BANKROLL="${3:-1000000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== 1. health ==="
python3 -c "
import requests, json
r=requests.get('$API/health', timeout=60)
print(json.dumps(r.json(), ensure_ascii=False, indent=2))
"

if [ \"${SKIP_SYNC:-0}\" != \"1\" ]; then
  if [ -z \"$SECRET\" ]; then
    echo \"ADMIN_SYNC_SECRET が無いため同期をスキップします。\"
    echo \"export ADMIN_SYNC_SECRET=... してから再実行するか SKIP_SYNC=1 で明示スキップ。\"
  else
    echo \"=== 2a. Supabase → Neon (制限中に副系へ書いた分を主系へ) ===\"
    python3 -c "
import requests, json, os
api='$API'
sec=os.environ.get('ADMIN_SYNC_SECRET','')
r=requests.post(api+'/admin/sync-supabase-to-neon', params={'token':sec}, timeout=600)
print('status', r.status_code)
try:
    d=r.json()
    print('ok', d.get('ok'))
    print((d.get('log') or d.get('detail') or '')[-2000:])
except Exception:
    print(r.text[:2000])
"
    echo \"=== 2b. Neon → Supabase (主系の最新を副系へバックアップ) ===\"
    python3 -c "
import requests, json, os
api='$API'
sec=os.environ.get('ADMIN_SYNC_SECRET','')
r=requests.post(api+'/admin/sync-neon-to-supabase', params={'token':sec}, timeout=600)
print('status', r.status_code)
try:
    d=r.json()
    print('ok', d.get('ok'))
    print((d.get('log') or d.get('detail') or '')[-2000:])
except Exception:
    print(r.text[:2000])
"
  fi
else
  echo \"=== 2. 同期スキップ (SKIP_SYNC=1) ===\"
fi

echo \"=== 3. warm-calibration ===\"
python3 -c "
import requests, time, json
api='$API'
for i in range(6):
    try:
        r=requests.post(api+'/purchases/warm-calibration', timeout=300)
        if r.status_code==200 and r.text.strip().startswith('{'):
            d=r.json()
            print('ok', d.get('ok'), 'total_s', d.get('total_seconds'))
            ho=d.get('high_odds_residual') or {}
            print('high_odds bands', list((ho.get('by_odds_band') or {}).keys()))
            break
        print('warm retry', i, r.status_code)
    except Exception as e:
        print('warm err', e)
    time.sleep(min(5*(2**i), 60))
"

if [ \"${SKIP_REPLAY:-0}\" != \"1\" ]; then
  echo \"=== 4. 全体再投票 limit=$LIMIT interval=${INTERVAL}s ===\"
  echo \"（件数が多いと数時間かかります。中断後は run_replay_continue.sh で再開可）\"
  python3 -u scraper/replay_settled.py --since all --limit \"$LIMIT\" --bankroll \"$BANKROLL\" --interval \"$INTERVAL\"
else
  echo \"=== 4. 再投票スキップ (SKIP_REPLAY=1) ===\"
fi

echo \"=== 5. 診断前に30秒待機 ===\"
sleep 30

echo \"=== 6. predicted-vs-actual (直近窓) ===\"
bash \"$ROOT/scraper/run_pva_recent.sh\" 24 200 || true

echo \"=== 7. predicted-vs-actual (全件要約) ===\"
python3 -c "
import requests, json
api='$API'
r=requests.get(api+'/purchases/diagnostics/predicted-vs-actual-return', timeout=180)
print('status', r.status_code)
if r.status_code==200 and r.text.strip().startswith('{'):
    d=r.json()
    o=d.get('overall') or {}
    print('n', o.get('bet_count'))
    print('pred_hit%', o.get('predicted_avg_prob_pct'), 'act_hit%', o.get('actual_hit_rate_pct'))
    ph=o.get('predicted_avg_prob_pct') or 0
    ah=o.get('actual_hit_rate_pct') or 0.01
    print('ratio', round(ph/max(ah,0.01), 3))
    print('pred_ROI%', o.get('stored_ev_predicted_roi_pct'), 'act_ROI%', o.get('actual_roi_pct'))
else:
    print((r.text or '')[:500])
"
echo \"=== 完了 ===\"
