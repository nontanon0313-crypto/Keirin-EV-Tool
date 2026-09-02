#!/data/data/com.termux/files/usr/bin/bash
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
SINCE="${1:-calibration_switch}"

python3 -u - "$API" "$SINCE" << 'PY'
import json, sys, requests

api, since = sys.argv[1], sys.argv[2]
params = {"since": since}

def get(path, timeout=180):
    r = requests.get(f"{api}{path}", params=params, timeout=timeout)
    return r.status_code, r

print(f"API={api} since={since}\n")

print("=== 1. 見送り件数 ===")
r = requests.get(f"{api}/races/skipped-bet-counts", timeout=60)
print(r.status_code, json.dumps(r.json(), ensure_ascii=False)[:900])
print()

print("=== 2. 購入群 vs EVマイナス除外 ===")
code, r = get("/purchases/diagnostics/filter-effectiveness")
print("status", code)
if code == 200:
    d = r.json()
    o = d.get("overall") or {}
    p, e = o.get("purchased") or {}, o.get("ev_negative_excluded") or {}
    print(f"購入: n={p.get('bet_count')} ROI={p.get('actual_roi_pct')}% 的中={p.get('actual_hit_rate_pct')}%")
    print(f"EVマイナス除外: n={e.get('bet_count')} ROI={e.get('actual_roi_pct')}% 的中={e.get('actual_hit_rate_pct')}% odds={e.get('average_odds')}")
    print(f"warning: {o.get('warning')}")
    for bt, v in (d.get("by_bet_type") or {}).items():
        pp, ee = v.get("purchased") or {}, v.get("ev_negative_excluded") or {}
        print(f"  {bt}: 購入ROI={pp.get('actual_roi_pct')} n={pp.get('bet_count')} | 除外ROI={ee.get('actual_roi_pct')} n={ee.get('bet_count')}")
print()

print("=== 3. 確率キャリブ格子（予測 vs 実績） ===")
code, r = get("/purchases/diagnostics/prob-calibration-grid")
print("status", code)
if code == 200:
    d = r.json()
    print(f"total_rows={d.get('total_rows')}")
    print("--- 全体 ---")
    for b in d.get("overall") or []:
        print(f"  {b.get('band')}: n={b.get('n')} pred={b.get('predicted_avg_pct')}% act={b.get('actual_hit_rate_pct')}% gap={b.get('gap_pt')}pt")
    print("--- 券種別 ---")
    for bt, bands in (d.get("by_bet_type") or {}).items():
        print(bt)
        for b in bands:
            if (b.get("n") or 0) == 0:
                continue
            print(f"  {b.get('band')}: n={b.get('n')} pred={b.get('predicted_avg_pct')} act={b.get('actual_hit_rate_pct')} gap={b.get('gap_pt')}")
else:
    print(r.text[:400])
print()

print("=== 4. EV帯詳細（中EV vs 高EV） ===")
code, r = get("/purchases/diagnostics/ev-band-detail")
print("status", code)
if code == 200:
    d = r.json()
    print("--- 購入(実stake) ---")
    for b in d.get("purchases_by_band") or []:
        print(f"  {b.get('band')}: n={b.get('bet_count')} ROI={b.get('actual_roi_pct')} hit={b.get('actual_hit_rate_pct')}")
    print("--- 見送り(仮想100円) ---")
    for b in d.get("skips_by_band") or []:
        print(f"  {b.get('band')}: n={b.get('bet_count')} ROI={b.get('actual_roi_pct')} hit={b.get('actual_hit_rate_pct')}")
    f = d.get("focus") or {}
    print("focus mid", f.get("purchase_mid_ev_120_150_equiv"))
    print("focus high", f.get("purchase_high_ev_150_plus_equiv"))
else:
    print(r.text[:400])
print()

print("=== 5. 捨てた的中（見送りで当たった） ===")
code, r = get("/purchases/diagnostics/discarded-hits")
print("status", code)
if code == 200:
    d = r.json()
    print(f"discarded_hits={d.get('discarded_hit_count')} virtual_return_sum={d.get('virtual_return_total_per_100yen')}")
    print("by_bet_type", d.get("by_bet_type"))
    print("by_ev_bucket", d.get("by_ev_bucket"))
    print("by_prob_bucket", d.get("by_prob_bucket"))
    print("top_reasons", d.get("top_reasons")[:8])
    print("--- top payout samples ---")
    for s in (d.get("top_hits_by_payout") or [])[:10]:
        print(f"  r{s.get('race_id')} {s.get('bet_type')} {s.get('combination')} ev={s.get('ev_pct')} p={s.get('win_prob')} pay={s.get('actual_payout_per_100')} | {s.get('reason')}")
else:
    print(r.text[:400])
print()

print("=== 6. Raw vs Calibrated (要約) ===")
code, r = get("/purchases/diagnostics/raw-vs-calibrated")
if code == 200:
    d = r.json()
    ov = d.get("overall") or {}
    print("raw", (ov.get("raw") or {}).get("actual_roi_pct"), "cal", (ov.get("calibrated") or {}).get("actual_roi_pct"))
    for bt, v in (d.get("by_bet_type") or {}).items():
        print(f"  {bt}: raw={ (v.get('raw') or {}).get('actual_roi_pct') } cal={ (v.get('calibrated') or {}).get('actual_roi_pct') } {v.get('calibration_verdict')}")
else:
    print(code, r.text[:300])
print("\n=== 完了 ===")
PY
