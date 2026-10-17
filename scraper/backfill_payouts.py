#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保存済みの結果JSON(scraper/data*/*.json)から公式の3連単払戻金をサーバーへ送り、回収率への影響を確認する。

  python3 scraper/backfill_payouts.py            # 送信して dry-run の影響だけ表示
  python3 scraper/backfill_payouts.py --apply    # 公式払戻金で既存の的中を精算し直す
何度実行しても安全(冪等)。
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path
import requests

API = os.environ.get("KEIRIN_API_BASE", os.environ.get("API_BASE", "https://keirin-ev-tool.onrender.com")).rstrip("/")
HERE = Path(__file__).resolve().parent
CHUNK = 500


def yen(v):
    m = re.search(r"[\d,]+", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


def collect():
    items, seen, bad = [], set(), 0
    dirs = sorted(p for p in HERE.glob("data*") if p.is_dir())
    for d in dirs:
        for f in d.glob("*.json"):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                bad += 1
                continue
            pays = [p for p in ((j.get("result") or {}).get("payouts") or []) if p.get("賭式") == "3連単"]
            if len(pays) != 1 or not (j.get("result") or {}).get("results"):
                continue
            y = yen(pays[0].get("払戻金"))
            if not y or y < 100:
                continue
            try:
                ref = f"oddspark:{j['jo_code']}:{j['kaisai_bi']}:{int(j['race_no'])}"
            except Exception:
                continue
            if ref in seen:
                continue
            seen.add(ref)
            items.append({"external_ref": ref, "payout_3t": y})
    return items, len(dirs), bad


def post(path, **kw):
    for attempt in range(6):
        try:
            r = requests.post(f"{API}{path}", timeout=180, **kw)
            if r.status_code in (429, 502, 503, 504):
                time.sleep(min(5 * 2 ** attempt, 60))
                continue
            return r
        except requests.RequestException as e:
            print(f"  通信エラー {type(e).__name__} → 再試行")
            time.sleep(min(5 * 2 ** attempt, 60))
    return None


def fetch_missing_from_site():
    """的中があるのに公式払戻金が未保存のレースを、結果ページから直接取得して送る。"""
    sys.path.insert(0, str(HERE))
    try:
        from keirin_oddspark_scraper import parse_result
    except Exception as e:
        print(f"  結果ページの取得機能を読み込めません: {type(e).__name__}")
        return
    try:
        r = requests.get(f"{API}/payouts/missing-wins", timeout=120)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"  未取得一覧の取得失敗: {type(e).__name__}")
        return
    refs, no_ref = [], 0
    for t in d.get("tiers", []):
        refs += t.get("refs", [])
        no_ref += t.get("no_external_ref", 0)
    refs = sorted(set(refs))
    print(f"\n的中があるのに公式払戻が未保存: {len(refs)}レース(external_ref無しで取得不能 {no_ref}レース)")
    got, fail = [], 0
    for k, ref in enumerate(refs, 1):
        try:
            _, jo, day, no = ref.split(":")
            res = parse_result(jo, day, int(no))
            pays = [p for p in (res.get("payouts") or []) if p.get("賭式") == "3連単"]
            y = yen(pays[0].get("払戻金")) if len(pays) == 1 else None
            if y and y >= 100:
                got.append({"external_ref": ref, "payout_3t": y})
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  取得失敗 {ref}: {type(e).__name__}")
        if k % 10 == 0:
            print(f"  取得 {k}/{len(refs)}")
        time.sleep(1.0)
    print(f"  取得できた {len(got)}件 / 失敗 {fail}件")
    for i in range(0, len(got), CHUNK):
        post("/payouts/backfill", json={"items": got[i:i + CHUNK]})


def main():
    apply = "--apply" in sys.argv
    items, nd, bad = collect()
    print(f"対象ディレクトリ{nd}個 / 公式3連単払戻あり {len(items)}レース(読めないJSON {bad}件)")
    if not items:
        print("送るデータがありません"); return 1
    upd = {}
    for i in range(0, len(items), CHUNK):
        r = post("/payouts/backfill", json={"items": items[i:i + CHUNK]})
        if r is None or r.status_code >= 400:
            print(f"  送信失敗 {getattr(r, 'status_code', None)} {getattr(r, 'text', '')[:200]}"); return 1
        for t in r.json().get("tiers", []):
            u = upd.setdefault(t["name"], [0, 0]); u[0] += t["races_found"]; u[1] += t["updated"]
        print(f"  送信 {min(i + CHUNK, len(items))}/{len(items)}")
    for n, (f, u) in upd.items():
        print(f"  {n}: 一致レース{f} / 更新{u}")
    if "--no-fetch" not in sys.argv:
        fetch_missing_from_site()
    r = post("/payouts/resettle", params={"dry_run": "false" if apply else "true"})
    if r is None or r.status_code >= 400:
        print(f"  精算確認失敗 {getattr(r, 'status_code', None)} {getattr(r, 'text', '')[:200]}"); return 1
    d = r.json(); t = d["total"]
    print(f"\n=== 公式払戻金での精算({'反映済み' if apply else '確認のみ・未反映'}) ===")
    print(f"的中 {t['wins']}件のうち公式払戻あり {t['wins_with_official']}件")
    print(f"回収率: {t['roi_before_pct']}% → {t['roi_after_pct']}%  (払戻 {t['payout_before']:,} → {t['payout_after']:,}円 / 投資 {t['stake']:,}円)")
    for x in d["tiers"]:
        s = x["stored_over_official_ratio"]
        print(f" [{x['name']}] 公式払戻の取得率{x['coverage_pct']}% 回収率{x['roi_before_pct']}→{x['roi_after_pct']}% 保存/公式の比 {s}")
        for b in x["biggest_overstatements"][:3]:
            print(f"    過大 race_id={b['race_id']} 保存{b['stored_payout']:,.0f}円 → 公式{b['official_payout']:,.0f}円")
    if d["tier_errors"]:
        print("読めなかった系統:", d["tier_errors"])
    if not apply:
        print("\n反映するなら: python3 scraper/backfill_payouts.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
