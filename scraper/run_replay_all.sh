#!/data/data/com.termux/files/usr/bin/bash
set -e
API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"
export API_BASE
cd "${HOME}/Keirin-EV-Tool"
python3 -u scraper/replay_settled.py --since all --limit 5000 --bankroll 1000000 "$@"
