#!/usr/bin/env bash
# 指定日(省略時は昨日)のデータ取得→DB登録→予想までを1コマンドでまとめて実行する。
#
# 使い方:
#   bash run_day.sh            # 昨日の分を処理
#   bash run_day.sh 20260826   # 日付を指定して処理
#
# どこのディレクトリから実行してもよい(このファイルがある場所を自動で基準にする)。

set -e
cd "$(dirname "$0")"

DATE="${1:-$(python -c 'from datetime import date, timedelta; print((date.today()-timedelta(days=1)).strftime("%Y%m%d"))')}"

echo "=== ${DATE} のデータを処理します ==="
echo ""

echo "--- 1/3 出走レース一覧を作成 ---"
python make_joblist.py --dates "${DATE}" --out data/joblist.txt

export KEIRIN_DATA_DIR="$PWD/data"

echo ""
echo "--- 2/3 データ取得(スクレイピング。数時間かかる場合があります) ---"
python -u auto_fetch.py

echo ""
echo "--- 3/3 DB登録→予想まで一括実行 ---"
python run_full_pipeline.py --dir data/ --concurrency 3

echo ""
echo "=== ${DATE} の処理が完了しました ==="
