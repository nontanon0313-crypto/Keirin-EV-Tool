#!/data/data/com.termux/files/usr/bin/bash
# 収支切り分け診断（よく使う確認を一括）
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

print(f"API={api} since={since}")
print()

# 1) counts
print("=== 1. 見送り件数 ===")
try:
    r = requests.get(f"{api}/races/skipped-bet-counts", timeout=60)
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False)[:800])
except Exception as e:
    print("ERR", e)
print()

# 2) filter-effectiveness overall + by type
print("=== 2. 購入群 vs EVマイナス除外 ===")
code, r = get("/purchases/diagnostics/filter-effectiveness")
print("status", code)
if code != 200:
    print(r.text[:500])
else:
    d = r.json()
    o = d.get("overall") or {}
    p, e = o.get("purchased") or {}, o.get("ev_negative_excluded") or {}
    print(f"購入: n={p.get('bet_count')} ROI={p.get('actual_roi_pct')}% 的中率={p.get('actual_hit_rate_pct')}%")
    print(f"EVマイナス除外: n={e.get('bet_count')} ROI={e.get('actual_roi_pct')}% 的中率={e.get('actual_hit_rate_pct')}% 平均オッズ={e.get('average_odds')}")
    print(f"warning: {o.get('warning')}")
    print("--- 券種別 ---")
    for bt, v in (d.get("by_bet_type") or {}).items():
        pp, ee = v.get("purchased") or {}, v.get("ev_negative_excluded") or {}
        print(
            f"{bt}: 購入 n={pp.get('bet_count')} ROI={pp.get('actual_roi_pct')} | "
            f"除外 n={ee.get('bet_count')} ROI={ee.get('actual_roi_pct')} | "
            f"{v.get('warning')}"
        )
print()

# 3) ev-bands
print("=== 3. EV帯別ROI ===")
code, r = get("/purchases/diagnostics/ev-bands")
print("status", code)
if code != 200:
    print(r.text[:500])
else:
    d = r.json()
    overall = d.get("overall") or {}
    print(f"purchase_count={d.get('purchase_count')} correlation={overall.get('ev_rank_correlation')} {overall.get('note')}")
    for b in overall.get("bands") or []:
        print(
            f"  {b.get('band')}: n={b.get('bet_count')} ROI={b.get('actual_roi_pct')} "
            f"hit={b.get('actual_hit_rate_pct')}% predEV={b.get('predicted_average_ev_pct')} "
            f"insuff={b.get('n_insufficient')}"
        )
    print("--- 券種別(要約: 件数>0の帯のみ) ---")
    for bt, info in (d.get("by_bet_type") or {}).items():
        bands = [b for b in (info.get("bands") or []) if (b.get("bet_count") or 0) > 0]
        if not bands:
            continue
        print(f"{bt} corr={info.get('ev_rank_correlation')}")
        for b in bands:
            print(f"  {b.get('band')}: n={b.get('bet_count')} ROI={b.get('actual_roi_pct')}")
print()

# 4) raw vs cal
print("=== 4. Raw vs Calibrated ===")
code, r = get("/purchases/diagnostics/raw-vs-calibrated")
print("status", code)
if code != 200:
    print(r.text[:800])
else:
    d = r.json()
    ov = d.get("overall") or {}
    raw, cal = ov.get("raw") or {}, ov.get("calibrated") or {}
    print(f"Raw ROI={raw.get('actual_roi_pct')} n={raw.get('bet_count')}")
    print(f"Cal ROI={cal.get('actual_roi_pct')} n={cal.get('bet_count')}")
    print(f"raw_only={ov.get('raw_only_count')} cal_only={ov.get('calibrated_only_count')} both={ov.get('both_count')}")
    print("--- 券種別 ---")
    for bt, v in (d.get("by_bet_type") or {}).items():
        rr, cc = v.get("raw") or {}, v.get("calibrated") or {}
        print(
            f"{bt}: rawROI={rr.get('actual_roi_pct')} calROI={cc.get('actual_roi_pct')} "
            f"verdict={v.get('calibration_verdict')} brier_r={v.get('brier_raw')} brier_c={v.get('brier_calibrated')}"
        )
print()

# 5) odds-drift
print("=== 5. オッズ乖離 ===")
code, r = get("/purchases/diagnostics/odds-drift")
print("status", code)
if code == 200:
    d = r.json()
    print(json.dumps({k: d[k] for k in d if k != "note"}, ensure_ascii=False)[:900])
else:
    print(r.text[:400])
print()
print("=== 完了 ===")
PY
