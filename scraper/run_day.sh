#!/usr/bin/env bash
# 指定日(省略時は昨日)の取得→DB登録→予想→投票記録→結果確定を1コマンドで実行する。
#
# 使い方:
#   bash ~/Keirin-EV-Tool/scraper/run_day.sh
#   bash ~/Keirin-EV-Tool/scraper/run_day.sh 20260819
#
# どこから実行しても、このスクリプトのある scraper/ を基準にする。
# パイプラインは必ず --date で絞るため、data/ に他日のJSONがあっても混ざらない。

set -euo pipefail
cd "$(dirname "$0")"

# Termux 等では python3 を優先
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "python3 が見つかりません" >&2
  exit 1
fi

DATE="${1:-$($PY -c 'from datetime import date, timedelta; print((date.today()-timedelta(days=1)).strftime("%Y%m%d"))')}"

if ! [[ "$DATE" =~ ^[0-9]{8}$ ]]; then
  echo "日付は YYYYMMDD で指定してください: $DATE" >&2
  exit 1
fi

export KEIRIN_DATA_DIR="${KEIRIN_DATA_DIR:-$PWD/data}"
mkdir -p "$KEIRIN_DATA_DIR"

echo "=== ${DATE} のデータを処理します (data=${KEIRIN_DATA_DIR}) ==="
echo ""

echo "--- 1/4 出走レース一覧を作成 (${DATE} のみ) ---"
$PY make_joblist.py --dates "${DATE}" --out "$KEIRIN_DATA_DIR/joblist.txt"

echo ""
echo "--- 2/4 データ取得(スクレイピング。件数により時間がかかります) ---"
$PY -u auto_fetch.py

echo ""
echo "--- 3/4 DB登録→予想→投票記録→結果確定 (${DATE} のJSONのみ) ---"
set +e
$PY run_full_pipeline.py --dir "$KEIRIN_DATA_DIR" --date "${DATE}" --concurrency 3
PIPELINE_EXIT=$?
set -e

if [ "$PIPELINE_EXIT" -eq 2 ]; then
  echo ""
  echo "########################################################"
  echo "# ⚠️  Gemini利用枠切れで ${DATE} の一部レースが未予想です"
  echo "#  枠が回復してから、以下のコマンドで同じ日付を"
  echo "#  もう一度実行してください(完了済み分は自動でスキップされます):"
  echo "#"
  echo "#  bash ~/Keirin-EV-Tool/scraper/run_day.sh ${DATE}"
  echo "########################################################"
  echo ""
  exit 2
elif [ "$PIPELINE_EXIT" -ne 0 ]; then
  echo "run_full_pipeline.pyが異常終了しました(終了コード: ${PIPELINE_EXIT})" >&2
  exit "$PIPELINE_EXIT"
fi

echo ""
echo "--- 4/4 壊れた着順の修復(車番以外が入った結果) ---"
# デプロイ済みなら修復APIを呼ぶ。未デプロイ・失敗しても全体は成功扱い
$PY - <<'PY' || true
import os, sys
try:
    import requests
except ImportError:
    sys.exit(0)
base = os.environ.get("API_BASE", "https://keirin-ev-tool.onrender.com").rstrip("/")
try:
    r = requests.post(f"{base}/races/repair-broken-results", params={"apply": True}, timeout=90)
    if r.status_code == 404:
        print("   (repair-broken-results 未デプロイのためスキップ)")
        sys.exit(0)
    r.raise_for_status()
    data = r.json()
    print(f"   壊れた結果: {data.get('broken_count', 0)}件をクリア(applied={data.get('applied')})")
except Exception as e:
    print(f"   修復スキップ: {e}")
PY

echo ""
echo "=== ${DATE} の処理が完了しました ==="
echo "集計確認例:"
echo "  $PY -c \"import requests,json; r=requests.get('https://keirin-ev-tool.onrender.com/purchases/stats',timeout=90); print(json.dumps(r.json(),ensure_ascii=False,indent=2)[:2000])\""
