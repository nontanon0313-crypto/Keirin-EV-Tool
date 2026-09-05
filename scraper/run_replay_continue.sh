#!/data/data/com.termux/files/usr/bin/bash
# 使い方: bash scraper/run_replay_continue.sh [件数] [間隔秒] [証拠金] [after_race_id(通常不要)]
# 旧データの再投票は「既に現行基準で再投票済みのレース」を自動で除外するので、
# 同じコマンドを繰り返し実行するだけで続きから進む(after_race_idの手動指定は不要)。
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
LIMIT="${1:-50}"
INTERVAL="${2:-2}"
BANKROLL="${3:-1000000}"
AFTER_RACE_ID="${4:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== warm ==="
python3 -c "
import requests, time, json
api='$API'
for i in range(6):
    try:
        r=requests.post(api+'/purchases/warm-calibration', timeout=300)
        if r.status_code==200 and r.text.strip().startswith('{'):
            print(r.status_code)
            print(json.dumps(r.json(), ensure_ascii=False)[:500])
            break
        print('warm retry', i, r.status_code)
    except Exception as e:
        print('warm err', e)
    time.sleep(min(5*(2**i), 60))
"

echo "=== replay limit=$LIMIT interval=${INTERVAL}s after_race_id=${AFTER_RACE_ID:-なし} ==="
if [ -n "$AFTER_RACE_ID" ]; then
  python3 -u scraper/replay_settled.py --since all --limit "$LIMIT" --bankroll "$BANKROLL" --interval "$INTERVAL" --after-race-id "$AFTER_RACE_ID"
else
  python3 -u scraper/replay_settled.py --since all --limit "$LIMIT" --bankroll "$BANKROLL" --interval "$INTERVAL"
fi

echo "=== 診断前に30秒待機(429緩和) ==="
sleep 30

echo "=== predicted-vs-actual ==="
python3 -c "
import requests, time, json
api='$API'
for i in range(5):
    try:
        r=requests.get(api+'/purchases/diagnostics/predicted-vs-actual-return', timeout=180)
        if r.status_code==200 and r.text.strip().startswith('{'):
            d=r.json()['overall']
            print('n', d['bet_count'])
            print('pred_hit%', round(d['predicted_avg_prob_pct'],2), 'act_hit%', round(d['actual_hit_rate_pct'],2))
            print('ratio', round(d['predicted_avg_prob_pct']/max(d['actual_hit_rate_pct'],0.01),2))
            print('pred_ROI%', round(d['stored_ev_predicted_roi_pct'],1), 'act_ROI%', round(d['actual_roi_pct'],1))
            print('expected_hits', round(d['probability_sum_expected_hits'],1), 'actual_hits', d['probability_sum_actual_hits'])
            break
        print('diag retry', i, r.status_code)
    except Exception as e:
        print('diag err', e)
    time.sleep(min(10*(2**i), 90))
"
echo "=== 完了 ==="
