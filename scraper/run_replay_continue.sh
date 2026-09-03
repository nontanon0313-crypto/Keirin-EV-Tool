#!/data/data/com.termux/files/usr/bin/bash
# 使い方: bash scraper/run_replay_continue.sh [件数]
# 先に warm → 指定件数 replay → 効果測定をまとめて実行
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
LIMIT="${1:-50}"
BANKROLL="${2:-1000000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== warm ==="
python3 -c "
import requests, json
r=requests.post('$API/purchases/warm-calibration', timeout=300)
print(r.status_code)
print(json.dumps(r.json(), ensure_ascii=False)[:500])
"

echo "=== replay limit=$LIMIT ==="
python3 -u scraper/replay_settled.py --since all --limit "$LIMIT" --bankroll "$BANKROLL"

echo "=== predicted-vs-actual ==="
python3 -c "
import requests, json
api='$API'
d=requests.get(api+'/purchases/diagnostics/predicted-vs-actual-return', timeout=180).json()['overall']
print('n', d['bet_count'])
print('pred_hit%', round(d['predicted_avg_prob_pct'],2), 'act_hit%', round(d['actual_hit_rate_pct'],2))
print('ratio', round(d['predicted_avg_prob_pct']/max(d['actual_hit_rate_pct'],0.01),2))
print('pred_ROI%', round(d['stored_ev_predicted_roi_pct'],1), 'act_ROI%', round(d['actual_roi_pct'],1))
print('expected_hits', round(d['probability_sum_expected_hits'],1), 'actual_hits', d['probability_sum_actual_hits'])
"

echo "=== winning-capture ==="
python3 -c "
import requests, json
api='$API'
r=requests.get(api+'/purchases/diagnostics/winning-capture', params={'since':'all','limit_races':100}, timeout=180)
print(r.status_code)
print(json.dumps(r.json(), ensure_ascii=False)[:1500])
"
echo "=== 完了 ==="
