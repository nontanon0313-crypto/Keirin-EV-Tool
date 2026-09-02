#!/data/data/com.termux/files/usr/bin/bash
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
echo "== skipped-bet-counts =="
python3 -c "import requests,json; r=requests.get('$API/races/skipped-bet-counts',timeout=60); print(r.status_code); print(json.dumps(r.json(),ensure_ascii=False,indent=2)[:2000])"
echo "== backfill-skip-results =="
python3 -c "import requests,json; r=requests.post('$API/races/backfill-skip-results',json={},timeout=600); print(r.status_code); print(json.dumps(r.json(),ensure_ascii=False,indent=2)[:1500])"
echo "== backfill-final-odds =="
python3 -c "import requests,json; r=requests.post('$API/races/backfill-final-odds',json={},timeout=600); print(r.status_code); print(json.dumps(r.json(),ensure_ascii=False,indent=2)[:1500])"
echo "== diagnostics =="
bash "$(dirname "$0")/run_diagnostics.sh" calibration_switch
