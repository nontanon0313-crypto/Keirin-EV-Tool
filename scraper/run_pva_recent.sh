#!/data/data/com.termux/files/usr/bin/bash
# 直近replay分だけの predicted-vs-actual
# 使い方:
#   bash scraper/run_pva_recent.sh           # 直近3時間
#   bash scraper/run_pva_recent.sh 6         # 直近6時間
#   bash scraper/run_pva_recent.sh 3 50      # 直近3時間かつ直近50レース
set -e
API="${API_BASE:-https://keirin-ev-tool.onrender.com}"
HOURS="${1:-3}"
LAST_N="${2:-}"
echo "API=$API hours=$HOURS last_n_races=${LAST_N:-all_in_window}"

python3 - <<PY
import requests, json
api = "$API"
params = {"hours": float("$HOURS")}
last_n = "$LAST_N".strip()
if last_n:
    params["last_n_races"] = int(last_n)
r = requests.get(
    api + "/purchases/diagnostics/predicted-vs-actual-return",
    params=params,
    timeout=180,
)
print("status", r.status_code)
if r.status_code != 200 or not (r.text or "").strip().startswith("{"):
    print((r.text or "")[:500])
    raise SystemExit(1)
d = r.json()
o = d["overall"]
pred = o.get("predicted_avg_prob_pct") or 0
act = o.get("actual_hit_rate_pct") or 0
ratio = round(pred / act, 2) if act > 0 else None
print("filter", d.get("filter_note"), "hours", d.get("hours"), "last_n_races", d.get("last_n_races"))
print("race_count", d.get("race_count"), "n", o.get("bet_count"))
print("pred_hit%", round(pred, 2), "act_hit%", round(act, 2), "ratio", ratio)
print("pred_ROI%", o.get("stored_ev_predicted_roi_pct"), "act_ROI%", o.get("actual_roi_pct"))
print("expected_hits", o.get("probability_sum_expected_hits"), "actual_hits", o.get("probability_sum_actual_hits"))
print("--- by_bet_type ---")
for bt, s in (d.get("by_bet_type") or {}).items():
    p = s.get("predicted_avg_prob_pct") or 0
    a = s.get("actual_hit_rate_pct") or 0
    rr = round(p / a, 2) if a > 0 else None
    print(
        f"{bt}: n={s.get('bet_count')} ratio={rr} "
        f"actROI={s.get('actual_roi_pct')} predROI={s.get('stored_ev_predicted_roi_pct')}"
    )
print("--- by_odds_band (n>0) ---")
for band, s in (d.get("by_odds_band") or {}).items():
    if not s.get("bet_count"):
        continue
    p = s.get("predicted_avg_prob_pct") or 0
    a = s.get("actual_hit_rate_pct") or 0
    rr = round(p / a, 2) if a > 0 else None
    print(f"{band}: n={s.get('bet_count')} ratio={rr} actROI={s.get('actual_roi_pct')}")
print("--- by_prob_band (n>0) ---")
for band, s in (d.get("by_prob_band") or {}).items():
    if not s.get("bet_count"):
        continue
    p = s.get("predicted_avg_prob_pct") or 0
    a = s.get("actual_hit_rate_pct") or 0
    rr = round(p / a, 2) if a > 0 else None
    print(
        f"{band}: n={s.get('bet_count')} ratio={rr} "
        f"actROI={s.get('actual_roi_pct')} predROI={s.get('stored_ev_predicted_roi_pct')}"
    )
PY
