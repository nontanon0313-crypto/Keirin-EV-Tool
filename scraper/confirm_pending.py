#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_full_pipeline.pyの結果確定バグ(--dir/--file経路で結果確定が一度も
呼ばれていなかった)により「未確定」のまま残っているレースを、
再予想(Gemini呼び出し)はせずに結果確定だけやり直す。

スクレイパーが取得済みのJSONファイル(data/配下)をそのまま使う。
予想・投票プランは既に前回の実行で記録済みなのでそのまま残し、
結果の確定処理だけをやり直す。

使い方:
  cd ~/Keirin-EV-Tool/scraper
  export KEIRIN_DATA_DIR=$PWD/data
  python confirm_pending.py --dir data/
"""
from __future__ import annotations
import argparse, glob, json, os, re
import requests

API_BASE = os.environ.get("KEIRIN_API_BASE", "https://keirin-ev-tool.onrender.com")


def extract_actual_result(race_json):
    """JSONから確定済みの1-2-3着(車番)を取り出す。結果が無ければNone。"""
    results = race_json.get("result", {}).get("results", [])
    by_place = {r["着順"]: r["車番"] for r in results}
    parts = [by_place[str(p)] for p in (1, 2, 3) if str(p) in by_place]
    if len(parts) != 3:
        return None
    return "-".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="レースJSONが入ったディレクトリ")
    args = ap.parse_args()

    race_file_re = re.compile(r"^\d{8}_\d+_\d{2}\.json$")
    files = sorted(
        f for f in glob.glob(os.path.join(args.dir, "*.json"))
        if race_file_re.match(os.path.basename(f))
    )
    print(f"対象ファイル数: {len(files)}件")

    fixed, no_result, error = 0, 0, 0
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            race_json = json.load(f)

        actual_result = extract_actual_result(race_json)
        if not actual_result:
            no_result += 1
            continue

        try:
            # 既存レースの再登録(冪等: 新規作成はされず、既存のrace_idがそのまま返る)
            r = requests.post(f"{API_BASE}/scraper-import/race", json=race_json, timeout=90)
            r.raise_for_status()
            race_id = r.json().get("race_id")
            if not race_id:
                print(f"  スキップ({os.path.basename(fp)}): race_idが取得できません")
                continue

            r2 = requests.post(
                f"{API_BASE}/races/{race_id}/confirm-result",
                params={"actual_result": actual_result},
                timeout=90,
            )
            r2.raise_for_status()
            conf = r2.json()
            print(
                f"  race_id={race_id} 確定: {actual_result} "
                f"(購入{conf['updated_count']}件・見送り{conf['skipped_updated_count']}件を判定)"
            )
            fixed += 1
        except Exception as e:
            print(f"  エラー({os.path.basename(fp)}): {e}")
            error += 1

    print(f"=== 完了: 確定{fixed}件 / 結果まだ無し{no_result}件 / エラー{error}件 ===")


if __name__ == "__main__":
    main()
