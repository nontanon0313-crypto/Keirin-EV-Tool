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

print("=== 10. ワイド中心・的中率寄り戦略（仮想100円） ===")
code, r = get("/purchases/diagnostics/stable-wide-strategies")
if code == 200:
    d = r.json()
    aw = d.get("actual_wide_purchases") or {}
    print(f"実購入ワイド: n={aw.get('bet_count')} ROI={aw.get('actual_roi_pct')} hit={aw.get('actual_hit_rate_pct')}")
    print(f"tested={d.get('strategies_tested')} breakeven={d.get('strategies_meeting_breakeven')} top_k={d.get('top_k_per_race')}")
    print("--- best ---")
    for s in (d.get("best_strategies") or [])[:10]:
        print(
            f"  p[{s.get('min_prob')}-{s.get('max_prob')}) odds[{s.get('min_odds')}-{s.get('max_odds')}] "
            f"minEV={s.get('min_ev_pct')} rank={s.get('rank')}: "
            f"n={s.get('bet_count')} ROI={s.get('actual_roi_pct')} hit={s.get('actual_hit_rate_pct')} "
            f"avg_o={s.get('avg_odds')} BE={s.get('meets_breakeven')}"
        )
    print("--- profitable only ---")
    for s in (d.get("profitable_strategies") or [])[:10]:
        print(
            f"  p[{s.get('min_prob')}-{s.get('max_prob')}) odds≤{s.get('max_odds')} "
            f"minEV={s.get('min_ev_pct')} rank={s.get('rank')}: "
            f"ROI={s.get('actual_roi_pct')} hit={s.get('actual_hit_rate_pct')} n={s.get('bet_count')}"
        )
else:
    print(code, r.text[:500])

print()
print("=== 11. 予測払戻 vs 実際払戻（同一購入集合） ===")
code, r = get("/purchases/diagnostics/predicted-vs-actual-return")
if code == 200:
    d = r.json()
    o = d.get("overall") or {}
    c = d.get("consistency") or {}

    print(
        f"n={o.get('bet_count')} "
        f"hit={o.get('hit_count')} "
        f"stake={o.get('stake_total')}"
    )

    print(
        f"prob_expected_hits={o.get('probability_sum_expected_hits')} "
        f"actual_hits={o.get('probability_sum_actual_hits')} "
        f"gap_hits={o.get('probability_gap_hits')}"
    )

    print(
        f"storedEV_pred_return={o.get('predicted_return_from_stored_ev')} "
        f"ROI={o.get('stored_ev_predicted_roi_pct')}"
    )

    print(
        f"prob×odds_pred_return={o.get('predicted_return_from_prob_odds')} "
        f"ROI={o.get('prob_odds_predicted_roi_pct')}"
    )

    print(
        f"actual_return={o.get('actual_return')} "
        f"ROI={o.get('actual_roi_pct')}"
    )

    print(
        f"gap storedEV-vs-prob×odds={c.get('stored_ev_vs_prob_odds_return_gap')} "
        f"prob×odds-vs-actual={c.get('prob_odds_vs_actual_return_gap')} "
        f"storedEV-vs-actual={c.get('stored_ev_vs_actual_return_gap')}"
    )

    print("--- by odds band ---")
    for band, s in (d.get("by_odds_band") or {}).items():
        if (s.get("bet_count") or 0) <= 0:
            continue
        print(
            f"  {band}: "
            f"n={s.get('bet_count')} "
            f"predROI={s.get('prob_odds_predicted_roi_pct')} "
            f"actualROI={s.get('actual_roi_pct')} "
            f"predP={s.get('predicted_avg_prob_pct')} "
            f"actualHit={s.get('actual_hit_rate_pct')} "
            f"avgOdds={s.get('avg_odds')}"
        )

    print("--- by EV band ---")
    for band, s in (d.get("by_ev_band") or {}).items():
        if (s.get("bet_count") or 0) <= 0:
            continue
        print(
            f"  {band}: "
            f"n={s.get('bet_count')} "
            f"storedEVROI={s.get('stored_ev_predicted_roi_pct')} "
            f"probOddsROI={s.get('prob_odds_predicted_roi_pct')} "
            f"actualROI={s.get('actual_roi_pct')} "
            f"predP={s.get('predicted_avg_prob_pct')} "
            f"actualHit={s.get('actual_hit_rate_pct')}"
        )

    print("--- by bet type ---")
    for bt, s in (d.get("by_bet_type") or {}).items():
        print(
            f"  {bt}: "
            f"n={s.get('bet_count')} "
            f"predROI={s.get('prob_odds_predicted_roi_pct')} "
            f"actualROI={s.get('actual_roi_pct')} "
            f"predP={s.get('predicted_avg_prob_pct')} "
            f"actualHit={s.get('actual_hit_rate_pct')}"
        )
else:
    print(code, r.text[:700])


print()
print("=== 12. 購入群限定の確率校正・選別バイアス ===")
code, r = get("/purchases/diagnostics/purchase-selection-calibration")

if code == 200:
    d = r.json()

    o = d.get("overall") or {}

    print(
        f"overall n={o.get('n')} "
        f"wins={o.get('wins')} "
        f"pred={o.get('predicted_avg_pct')}% "
        f"act={o.get('actual_hit_rate_pct')}% "
        f"gap={o.get('gap_pt')}pt "
        f"expected_hits={o.get('expected_hits')} "
        f"actual_hits={o.get('actual_hits')} "
        f"p_lower={o.get('p_value_lower_tail_pct')}%"
    )

    print("--- 購入群: 確率帯別 ---")

    for band, s in (d.get("by_probability_band") or {}).items():
        if not s or s.get("n", 0) == 0:
            continue

        print(
            f"  {band}: "
            f"n={s.get('n')} "
            f"pred={s.get('predicted_avg_pct')} "
            f"act={s.get('actual_hit_rate_pct')} "
            f"gap={s.get('gap_pt')} "
            f"expHits={s.get('expected_hits')} "
            f"wins={s.get('wins')} "
            f"ROI_pred={s.get('predicted_roi_pct')} "
            f"ROI_act={s.get('actual_roi_pct')} "
            f"p={s.get('p_value_lower_tail_pct')}%"
        )

    print("--- 購入群: 確率帯 × オッズ帯 ---")

    for key, s in (d.get("by_probability_x_odds") or {}).items():
        if s.get("n", 0) < 5:
            continue

        print(
            f"  {key}: "
            f"n={s.get('n')} "
            f"pred={s.get('predicted_avg_pct')} "
            f"act={s.get('actual_hit_rate_pct')} "
            f"gap={s.get('gap_pt')} "
            f"wins={s.get('wins')} "
            f"avgOdds={s.get('avg_odds')} "
            f"ROI_pred={s.get('predicted_roi_pct')} "
            f"ROI_act={s.get('actual_roi_pct')}"
        )

    print("--- 購入群: EV帯 × オッズ帯 ---")

    for key, s in (d.get("by_ev_x_odds") or {}).items():
        if s.get("n", 0) < 5:
            continue

        print(
            f"  {key}: "
            f"n={s.get('n')} "
            f"pred={s.get('predicted_avg_pct')} "
            f"act={s.get('actual_hit_rate_pct')} "
            f"gap={s.get('gap_pt')} "
            f"wins={s.get('wins')} "
            f"avgOdds={s.get('avg_odds')} "
            f"ROI_pred={s.get('predicted_roi_pct')} "
            f"ROI_act={s.get('actual_roi_pct')}"
        )

    print("--- 過大評価が大きいセル n>=10 ---")

    for s in (d.get("most_overpredicted_cells") or [])[:20]:
        print(
            f"  {s.get('cell')}: "
            f"n={s.get('n')} "
            f"pred={s.get('predicted_avg_pct')} "
            f"act={s.get('actual_hit_rate_pct')} "
            f"gap={s.get('gap_pt')} "
            f"expHits={s.get('expected_hits')} "
            f"wins={s.get('wins')} "
            f"p={s.get('p_value_lower_tail_pct')}%"
        )

    print("--- レース単位参考 ---")

    rr = d.get("race_level_reference") or {}

    print(
        f"races={rr.get('race_count')} "
        f"hitRaces={rr.get('races_with_at_least_one_hit')} "
        f"raceHitRate={rr.get('race_hit_rate_pct')}% "
        f"probSum={rr.get('sum_of_purchase_probabilities')}"
    )

else:
    print(code, r.text[:1000])


print("\n=== 完了 ===")


PY
