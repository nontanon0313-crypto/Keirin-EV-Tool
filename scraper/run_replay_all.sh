#!/data/data/com.termux/files/usr/bin/bash
# 全確定レース 再投票→再結果検証（進捗つき）
set -e
API_BASE="${API_BASE:-https://keirin-ev-tool.onrender.com}"
export API_BASE
cd "${HOME}/Keirin-EV-Tool"
python3 -u scraper/replay_settled.py --since all --limit 5000 --bankroll 1000000 "$@"
