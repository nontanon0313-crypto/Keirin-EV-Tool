#!/data/data/com.termux/files/usr/bin/bash
# 使い方: bash scraper/run_replay_continue.sh [件数] [間隔秒] [証拠金] [after_race_id(通常不要)] [include_already_replayed(true/false、既定false)]
# include_already_replayed=true を指定すると、既にCALIBRATION_SWITCH_AT以降の
# 購入/見送り記録があるレースも再投票対象に含める(予想ロジック変更後に
# 全件を新ロジックで作り直したい場合に使う)。
# 対象0件なら warm/診断をせずに即終了する。
# ループ例:
#   while bash scraper/run_replay_continue.sh 30 3; do sleep 10; done
#   → 残0でスクリプトが exit 1 以外の「対象なし終了」をするので、下の終了コードに注意
#   推奨ループ(ロジック変更後の全件作り直し):
#   while true; do
#     LEFT=$(python3 -c "import requests; print(requests.get(\"$API_BASE/races/replay-settled/targets\",params={\"since\":\"all\",\"limit\":1,\"exclude_already_replayed\":\"false\"},timeout=90).json().get(\"total\",0))")
#     echo "残り=$LEFT"; [ "$LEFT" -le 0 ] && break
#     bash scraper/run_replay_continue.sh 30 3 1000000 "" true || true
#     sleep 15
#   done
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
LIMIT="${1:-50}"
INTERVAL="${2:-2}"
BANKROLL="${3:-1000000}"
AFTER_RACE_ID="${4:-}"
INCLUDE_ALREADY_REPLAYED="${5:-false}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== 残件数確認 ==="
LEFT=$(python3 -c "
import requests, sys
api='$API'
params={'since':'all','limit':1}
after='$AFTER_RACE_ID'
if after.strip():
    params['after_race_id']=int(after)
if '$INCLUDE_ALREADY_REPLAYED' == 'true':
    params['exclude_already_replayed']='false'
d=requests.get(api+'/races/replay-settled/targets', params=params, timeout=90).json()
print(d.get('total', 0))
")
echo "残り ${LEFT} 件 (limit=$LIMIT で処理)"
if [ "${LEFT:-0}" -le 0 ]; then
  echo "対象0件のため終了(warm/診断はスキップ)"
  exit 0
fi

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

echo "=== replay limit=$LIMIT interval=${INTERVAL}s after_race_id=${AFTER_RACE_ID:-なし} include_already_replayed=${INCLUDE_ALREADY_REPLAYED} ==="
EXTRA_ARGS=""
if [ "$INCLUDE_ALREADY_REPLAYED" = "true" ]; then
  EXTRA_ARGS="--include-already-replayed"
fi
if [ -n "$AFTER_RACE_ID" ]; then
  python3 -u scraper/replay_settled.py --since all --limit "$LIMIT" --bankroll "$BANKROLL" --interval "$INTERVAL" --after-race-id "$AFTER_RACE_ID" $EXTRA_ARGS
else
  python3 -u scraper/replay_settled.py --since all --limit "$LIMIT" --bankroll "$BANKROLL" --interval "$INTERVAL" $EXTRA_ARGS
fi

echo "=== 処理後の残件数 ==="
LEFT2=$(python3 -c "
import requests
params={'since':'all','limit':1}
if '$INCLUDE_ALREADY_REPLAYED' == 'true':
    params['exclude_already_replayed']='false'
d=requests.get('$API/races/replay-settled/targets', params=params, timeout=90).json()
print(d.get('total', 0))
")
echo "残り ${LEFT2} 件"
if [ "${LEFT2:-0}" -le 0 ]; then
  echo "全件完了。診断のみ実行して終了"
fi

echo "=== 診断前に15秒待機(429緩和) ==="
sleep 15

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
