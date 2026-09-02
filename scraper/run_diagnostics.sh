#!/data/data/com.termux/files/usr/bin/bash
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
SINCE="${1:-calibration_switch}"

python3 -u - "$API" "$SINCE" << 'PY'
import json, sys, requests

api, since = sys.argv[1], sys.argv[2]
params = {"since": since}

def get(path, timeout=240):
    r = requests.get(f"{api}{path}", params=params, timeout=timeout)
    return r.status_code, r

print(f"API={api} since={since}\n")

print("=== 1. 見送り件数 ===")
r = requests.get(f"{api}/races/skipped-bet-counts", timeout=60)
print(r.status_code, json.dumps(r.json(), ensure_ascii=False)[:700])
print()

print("=== 2. 購入 vs EVマイナス除外 ===")
code, r = get("/purchases/diagnostics/filter-effectiveness")
if code == 200:
    d = r.json(); o = d.get("overall") or {}
    p, e = o.get("purchased") or {}, o.get("ev_negative_excluded") or {}
    print(f"購入 n={p.get('bet_count')} ROI={p.get('actual_roi_pct')} | 除外 n={e.get('bet_count')} ROI={e.get('actual_roi_pct')}")
    print("warning:", o.get("warning"))
else:
    print(code, r.text[:300])
print()

print("=== 3. 確率格子 ===")
code, r = get("/purchases/diagnostics/prob-calibration-grid")
if code == 200:
    d = r.json()
    for b in d.get("overall") or []:
        print(f"  {b.get('band')}: n={b.get('n')} pred={b.get('predicted_avg_pct')} act={b.get('actual_hit_rate_pct')} gap={b.get('gap_pt')}")
else:
    print(code, r.text[:300])
print()

print("=== 4. ① 中EV vs 高EV（購入実stake） ===")
code, r = get("/purchases/diagnostics/mid-vs-high-ev")
if code == 200:
    d = r.json()
    for k in ["all_purchases", "mid_ev_only_20_to_50", "high_ev_only_50_plus", "other_ev"]:
        v = d.get(k) or {}
        print(f"  {k}: n={v.get('bet_count')} ROI={v.get('actual_roi_pct')} hit={v.get('actual_hit_rate_pct')}")
else:
    print(code, r.text[:400])
print()

print("=== 5. ② ステージゲート無し仮想（ステージ不足で落とした分） ===")
code, r = get("/purchases/diagnostics/stage-gate-off-virtual")
if code == 200:
    d = r.json()
    print(f"stage_skips={d.get('stage_skip_count')} discarded_hits={d.get('discarded_hit_count')}")
    v = d.get("stage_gated_skips_virtual_100") or {}
    print(f"  virtual ROI={v.get('actual_roi_pct')} n={v.get('bet_count')} hit={v.get('actual_hit_rate_pct')}")
    print("  by_bet_type:")
    for bt, st in (d.get("by_bet_type") or {}).items():
        print(f"    {bt}: n={st.get('bet_count')} ROI={st.get('actual_roi_pct')} hit={st.get('actual_hit_rate_pct')}")
    op = d.get("order_sensitive_purchases_actual") or {}
    print(f"  実際の着順券購入: n={op.get('bet_count')} ROI={op.get('actual_roi_pct')}")
else:
    print(code, r.text[:400])
print()

print("=== 6. ③ EVマイナス再計算（的中分） ===")
code, r = get("/purchases/diagnostics/ev-negative-recheck")
if code == 200:
    d = r.json()
    print(f"ev_neg_skips={d.get('ev_neg_skip_count')} hits={d.get('ev_neg_hit_count')}")
    print("hit_buckets", d.get("hit_recheck_buckets"))
    print("all_buckets", d.get("all_ev_neg_recheck_buckets"))
    print("--- top hits ---")
    for s in (d.get("top_hits") or [])[:12]:
        print(
            f"  r{s.get('race_id')} {s.get('bet_type')} {s.get('combination')} "
            f"storedEV={s.get('stored_ev_pct')} re={s.get('recomputed_ev_pct')} "
            f"odds={s.get('odds')} p={s.get('prob')} pay={s.get('actual_payout_per_100')} [{s.get('label')}]"
        )
else:
    print(code, r.text[:500])
print()

print("=== 7. 捨てた的中（要約） ===")
code, r = get("/purchases/diagnostics/discarded-hits")
if code == 200:
    d = r.json()
    print(f"n={d.get('discarded_hit_count')} return_sum={d.get('virtual_return_total_per_100yen')}")
    print("by_ev", d.get("by_ev_bucket"))
    print("reasons", (d.get("top_reasons") or [])[:5])
else:
    print(code, r.text[:300])

print("=== 8. race-plan が最大化しているもの ===")
code, r = get("/purchases/diagnostics/race-plan-design")
if code == 200:
    d = r.json()
    print("primary_rank:", d.get("objective", {}).get("primary_rank"))
    print("in_practice:", d.get("what_it_maximizes_in_practice"))
    print("not_optimizing:", d.get("what_it_does_not_optimize"))
else:
    print(code, r.text[:300])
print()

print("=== 9. 順位付けルール仮想比較（同一プール・仮想100円） ===")
code, r = get("/purchases/diagnostics/race-plan-rank-compare")
if code == 200:
    d = r.json()
    print(f"top_k={d.get('top_k')} min_ev={d.get('min_ev_pct')} races={d.get('race_count_with_pool')}")
    for pol in d.get("policies") or []:
        print(
            f"  {pol.get('policy')}: n={pol.get('bet_count')} ROI={pol.get('actual_roi_pct')} "
            f"hit={pol.get('actual_hit_rate_pct')} avg_odds={pol.get('avg_odds')} avg_ev={pol.get('avg_ev_pct')}"
        )
    print("meanings:", d.get("policy_meanings"))
else:
    print(code, r.text[:500])
print("\n=== 完了 ===")

PY
