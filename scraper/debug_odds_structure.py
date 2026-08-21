#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2車単・ワイドが毎回同じ件数で理論値に届かない件の調査用スクリプト。

パース結果だけでなく、表の各行の「生のセル内容」も一緒に出力する。
これにより、パースのどこで実際にズレているのか(特定の行だけ抜けているのか、
行の途中から数値がズレているのか等)を直接確認できる。

使い方:
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 6   # 2車単
  python debug_odds_structure.py --jo 44 --date 20260818 --race 1 --bet-type 7   # ワイド

出力は data/debug_{bet_type名}_{日付}_{場}_{レース}.json に保存される。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keirin_oddspark_scraper import get_soup, _parse_raw_grid, BASE, BET_TYPES, OUTPUT_DIR


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jo", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--race", required=True, type=int)
    ap.add_argument("--bet-type", required=True, type=int, help="5=2車複 6=2車単 7=ワイド 8=3連複 9=3連単")
    args = ap.parse_args()

    url = f"{BASE}/Odds.do"
    params = {"joCode": args.jo, "kaisaiBi": args.date, "raceNo": args.race, "betType": args.bet_type}
    print(f"取得URL: {url}?joCode={args.jo}&kaisaiBi={args.date}&raceNo={args.race}&betType={args.bet_type}")

    soup = get_soup(url, params, retries=4)
    grid, debug_rows = _parse_raw_grid(soup, debug=True)

    name = BET_TYPES.get(args.bet_type, str(args.bet_type))
    print(f"\n=== {name} ===")
    print(f"検出したヘッダー: {debug_rows[0]['header'] if debug_rows else '(行なし)'}")
    print(f"合計取得件数: {len(grid)}")
    print()
    for row in debug_rows:
        print(f"行車番={row['row_car']}  生セル数={len(row['raw_cells'])}  変換後の件数={len(row['parsed'])}")
        print(f"  生セル内容: {row['raw_cells']}")
        print(f"  変換結果  : {[(e['col_car'], e['odds']) for e in row['parsed']]}")
        print()

    out = {
        "jo_code": args.jo, "kaisai_bi": args.date, "race_no": args.race,
        "bet_type": name, "bet_code": args.bet_type,
        "total_count": len(grid),
        "rows": debug_rows,
    }
    fname = f"debug_{name}_{args.date}_{args.jo}_{args.race:02d}.json"
    path = os.path.join(OUTPUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"詳細を保存しました: {path}")


if __name__ == "__main__":
    main()
