#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
オッズ表の取得件数が理論値に届かない件の調査用スクリプト。

パース結果だけでなく、表の各行の「生のセル内容」、および
フィルタで弾かれた行も含めた「表の全<tr>の中身」を出力する。
これにより、特定の行がまるごと消えている場合に、
・HTML自体にその行が無いのか
・パース側で誤って弾かれているだけなのか
を区別できる。

使い方:
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 6   # 2車単
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 7   # ワイド
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 9 --axis 1   # 3連単(軸車番1)
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 8 --axis 1   # 3連複(軸車番1)

軸ベースの券種(--bet-type 8 or 9)は --axis で軸車番(1〜9)を指定する。

出力は data/debug_{bet_type名}_{日付}_{場}_{レース}[_axis{n}].json に保存される。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keirin_oddspark_scraper import get_soup, _parse_raw_grid, BASE, BET_TYPES, OUTPUT_DIR, AXIS_BASED_BET_TYPES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jo", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--race", required=True, type=int)
    ap.add_argument("--bet-type", required=True, type=int, help="5=2車複 6=2車単 7=ワイド 8=3連複 9=3連単")
    ap.add_argument("--axis", type=int, default=None, help="軸車番(bet-typeが8か9のとき必須)")
    args = ap.parse_args()

    if args.bet_type in AXIS_BASED_BET_TYPES and args.axis is None:
        ap.error(f"bet-type={args.bet_type}は軸ベースの券種です。--axis を指定してください")

    url = f"{BASE}/Odds.do"
    params = {"joCode": args.jo, "kaisaiBi": args.date, "raceNo": args.race, "betType": args.bet_type}
    if args.axis is not None:
        params["jikuCode"] = "1"
        params["shaban"] = str(args.axis)
    print(f"取得URL: {url}?{'&'.join(f'{k}={v}' for k, v in params.items())}")

    soup = get_soup(url, params, retries=4)
    grid, debug_rows, all_raw_rows = _parse_raw_grid(soup, debug=True)

    name = BET_TYPES.get(args.bet_type, str(args.bet_type))
    label = f"{name}" + (f" (軸車番={args.axis})" if args.axis is not None else "")
    print(f"\n=== {label} ===")
    print(f"表の<tr>総数(フィルタ前): {len(all_raw_rows)}")
    print(f"検出したヘッダー: {debug_rows[0]['header'] if debug_rows else '(データ行なし)'}")
    print(f"合計取得件数: {len(grid)}")

    print("\n--- 表の全<tr>の生の中身(フィルタで弾かれた行を含む) ---")
    for r in all_raw_rows:
        print(f"  tr[{r['row_index']}]: {r['cells']}")

    print("\n--- データ行として認識され、パースされた内容 ---")
    for row in debug_rows:
        print(f"行車番={row['row_car']}  生セル数={len(row['raw_cells'])}  変換後の件数={len(row['parsed'])}")
        print(f"  生セル内容: {row['raw_cells']}")
        print(f"  変換結果  : {[(e['col_car'], e['odds']) for e in row['parsed']]}")
        print()

    out = {
        "jo_code": args.jo, "kaisai_bi": args.date, "race_no": args.race,
        "bet_type": name, "bet_code": args.bet_type, "axis": args.axis,
        "total_count": len(grid),
        "all_raw_rows": all_raw_rows,
        "parsed_rows": debug_rows,
    }
    suffix = f"_axis{args.axis}" if args.axis is not None else ""
    fname = f"debug_{name}_{args.date}_{args.jo}_{args.race:02d}{suffix}.json"
    path = os.path.join(OUTPUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"詳細を保存しました: {path}")


if __name__ == "__main__":
    main()

