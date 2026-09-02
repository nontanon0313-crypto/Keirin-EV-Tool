#!/data/data/com.termux/files/usr/bin/bash
# zip反映→commit→push（Downloadsの最新replay zip想定）
set -e
cd "${HOME}/Keirin-EV-Tool"
ZIP="${1:-/sdcard/Download/keirin_replay_progress_20260902.zip}"
TMP="${HOME}/keirin_patch_tmp"
rm -rf "$TMP"
unzip -o "$ZIP" -d "$TMP"
cp "$TMP/app/routers/races.py" app/routers/
cp -f "$TMP/scraper/replay_settled.py" scraper/ 2>/dev/null || true
cp -f "$TMP/scraper/run_replay_all.sh" scraper/ 2>/dev/null || true
git add app/routers/races.py scraper/replay_settled.py scraper/run_replay_all.sh 2>/dev/null || git add app/routers/races.py
git commit -m "feat: replay進捗表示・全件対象・targets API" || true
git push
rm -rf "$TMP"
echo "deploy pushed. Render完了後: bash ~/Keirin-EV-Tool/scraper/run_replay_all.sh"
