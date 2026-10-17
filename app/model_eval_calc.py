"""モデル評価の純粋計算(DB非依存・単体テスト可能)。

ticket_eval : 買い目単位(実購入+見送り評価)の予想勝率の当たり具合
race_eval   : レース単位でAIの1着確率と市場(3連単オッズ)の1着確率を比較
build_hints : 結果から改善の手がかりを日本語で列挙
"""
import math

_EPS = 1e-6


def wilson(k: int, n: int, z: float = 1.96):
    if n <= 0:
        return (None, None)
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def _band(edges, v):
    for i in range(len(edges) - 1):
        if edges[i] <= v < edges[i + 1]:
            return i
    return len(edges) - 2


def _label(edges, i, unit="%", scale=1.0):
    lo, hi = edges[i] * scale, edges[i + 1] * scale
    if hi >= 1e8:
        return f"{lo:g}{unit}以上"
    return f"{lo:g}〜{hi:g}{unit}"


P_EDGES = [0.0, 0.005, 0.01, 0.02, 0.03, 0.04, 0.06, 0.08, 0.12, 1.01]
EV_EDGES = [-1e9, 0.0, 20.0, 50.0, 100.0, 200.0, 1e9]
ODDS_EDGES = [0.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1e9]
SHRINK_M = 100  # 補正案の縮約強さ(擬似サンプル数)


def ticket_eval(rows):
    """rows: iterable of (p, y, stake, payout, odds, ev_pct)。
    p=予想勝率(0-1), y=的中(1/0), stake>0なら実購入。"""
    data = [r for r in rows if r[0] is not None and 0 < r[0] <= 1]
    n = len(data)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in data if r[1])
    mean_p = sum(r[0] for r in data) / n
    hit = wins / n
    brier = sum((r[0] - (1 if r[1] else 0)) ** 2 for r in data) / n
    ll = -sum(math.log(max(r[0], _EPS)) if r[1] else math.log(max(1 - r[0], _EPS)) for r in data) / n
    b0 = hit * (1 - hit)
    ll0 = -(hit * math.log(max(hit, _EPS)) + (1 - hit) * math.log(max(1 - hit, _EPS))) if 0 < hit < 1 else None
    overall = {
        "n": n, "wins": wins,
        "mean_pred_pct": round(mean_p * 100, 3), "hit_pct": round(hit * 100, 3),
        "calibration_ratio": round(hit / mean_p, 3) if mean_p else None,
        "brier": round(brier, 5),
        "brier_skill_vs_const": round(1 - brier / b0, 4) if b0 > 0 else None,
        "logloss": round(ll, 5), "logloss_const": round(ll0, 5) if ll0 is not None else None,
    }

    bands = {}
    for r in data:
        bands.setdefault(_band(P_EDGES, r[0]), []).append(r)
    p_bands = []
    for i in sorted(bands):
        g = bands[i]
        gn = len(g)
        gw = sum(1 for r in g if r[1])
        gp = sum(r[0] for r in g) / gn
        lo, hi = wilson(gw, gn)
        if gn < 30:
            verdict = "サンプル少"
        elif hi < gp:
            verdict = "過大評価"
        elif lo > gp:
            verdict = "過小評価"
        else:
            verdict = "概ね一致"
        post = (gw + SHRINK_M * gp) / (gn + SHRINK_M)
        buy = [r for r in g if (r[2] or 0) > 0]
        stake = sum(r[2] for r in buy)
        payout = sum((r[3] or 0) for r in buy)
        p_bands.append({
            "band": _label(P_EDGES, i, "%", 100), "n": gn, "wins": gw,
            "mean_pred_pct": round(gp * 100, 3), "hit_pct": round(gw / gn * 100, 3),
            "ratio": round((gw / gn) / gp, 3) if gp else None,
            "ci95_low_pct": round(lo * 100, 3), "ci95_high_pct": round(hi * 100, 3),
            "verdict": verdict,
            "suggested_factor": round(post / gp, 3) if (gp and gn >= 30) else None,
            "buy_n": len(buy), "roi_pct": round(payout / stake * 100, 1) if stake > 0 else None,
        })

    buy_all = [r for r in data if (r[2] or 0) > 0]
    ev_bands, odds_bands, purchased = [], [], None
    if buy_all:
        stake = sum(r[2] for r in buy_all)
        payout = sum((r[3] or 0) for r in buy_all)
        exp_pay = sum(r[2] * (1 + (r[5] or 0) / 100.0) for r in buy_all if r[5] is not None)
        exp_stake = sum(r[2] for r in buy_all if r[5] is not None)
        purchased = {
            "n": len(buy_all), "wins": sum(1 for r in buy_all if r[1]),
            "stake": round(stake), "payout": round(payout),
            "roi_pct": round(payout / stake * 100, 1) if stake else None,
            "expected_roi_pct": round(exp_pay / exp_stake * 100, 1) if exp_stake else None,
        }
        for edges, key, out, unit in ((EV_EDGES, 5, ev_bands, "%"), (ODDS_EDGES, 4, odds_bands, "倍")):
            gs = {}
            for r in buy_all:
                if r[key] is None:
                    continue
                gs.setdefault(_band(edges, r[key]), []).append(r)
            for i in sorted(gs):
                g = gs[i]
                st = sum(r[2] for r in g)
                po = sum((r[3] or 0) for r in g)
                gw = sum(1 for r in g if r[1])
                item = {
                    "band": _label(edges, i, unit) if i not in (0,) or key != 5 else f"{edges[1]:g}{unit}未満",
                    "n": len(g), "wins": gw, "hit_pct": round(gw / len(g) * 100, 3),
                    "mean_pred_pct": round(sum(r[0] for r in g) / len(g) * 100, 3),
                    "stake": round(st), "roi_pct": round(po / st * 100, 1) if st else None,
                }
                if key == 5:
                    ep = sum(r[2] * (1 + r[5] / 100.0) for r in g)
                    item["expected_roi_pct"] = round(ep / st * 100, 1) if st else None
                out.append(item)
    return {"overall": overall, "by_pred_band": p_bands, "purchased": purchased,
            "by_ev_band": ev_bands, "by_odds_band": odds_bands}


R_EDGES = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.01]


def _norm(d):
    s = sum(v for v in d.values() if v and v > 0)
    return {k: (v / s) for k, v in d.items() if v and v > 0} if s > 0 else {}


def race_eval(races):
    """races: iterable of dict(winner=int, model={car:prob}, market={car:weight})"""
    used, skipped = [], 0
    for r in races:
        m, k, w = _norm(r["model"]), _norm(r["market"]), r["winner"]
        if len(m) < 2 or len(k) < 2 or w not in m or w not in k:
            skipped += 1
            continue
        used.append((m, k, w))
    n = len(used)
    if n == 0:
        return {"n": 0, "skipped": skipped}

    def ll(q, w):
        return -math.log(max(q.get(w, 0.0), _EPS))

    def brier(q, w, cars):
        return sum((q.get(c, 0.0) - (1.0 if c == w else 0.0)) ** 2 for c in cars)

    llm = [ll(m, w) for m, k, w in used]
    llk = [ll(k, w) for m, k, w in used]
    bm = [brier(m, w, set(m) | set(k)) for m, k, w in used]
    bk = [brier(k, w, set(m) | set(k)) for m, k, w in used]
    top_m = sum(1 for m, k, w in used if max(m, key=m.get) == w)
    top_k = sum(1 for m, k, w in used if max(k, key=k.get) == w)
    d = [b - a for a, b in zip(llm, llk)]  # 正=モデルが市場より良い
    md = sum(d) / n
    sd = math.sqrt(sum((x - md) ** 2 for x in d) / (n - 1)) if n > 1 else 0.0
    t = md / (sd / math.sqrt(n)) if sd > 0 else 0.0

    grid = []
    for a10 in range(0, 11):
        a = a10 / 10
        tot = 0.0
        for m, k, w in used:
            cars = set(m) | set(k)
            q = {c: a * m.get(c, 0.0) + (1 - a) * k.get(c, 0.0) for c in cars}
            tot += ll(q, w)
        grid.append((a, tot / n))
    best_a, best_ll = min(grid, key=lambda x: x[1])

    pairs = {}
    for m, k, w in used:
        for c, pr in m.items():
            pairs.setdefault(_band(R_EDGES, pr), []).append((pr, 1 if c == w else 0))
    cal = []
    for i in sorted(pairs):
        g = pairs[i]
        gn, gw = len(g), sum(x[1] for x in g)
        gp = sum(x[0] for x in g) / gn
        lo, hi = wilson(gw, gn)
        cal.append({
            "band": _label(R_EDGES, i, "%", 100), "n": gn, "wins": gw,
            "mean_pred_pct": round(gp * 100, 2), "hit_pct": round(gw / gn * 100, 2),
            "ci95_low_pct": round(lo * 100, 2), "ci95_high_pct": round(hi * 100, 2),
            "verdict": "サンプル少" if gn < 30 else ("過大評価" if hi < gp else ("過小評価" if lo > gp else "概ね一致")),
        })
    return {
        "n": n, "skipped": skipped,
        "logloss_model": round(sum(llm) / n, 5), "logloss_market": round(sum(llk) / n, 5),
        "brier_model": round(sum(bm) / n, 5), "brier_market": round(sum(bk) / n, 5),
        "top1_model_pct": round(top_m / n * 100, 2), "top1_market_pct": round(top_k / n * 100, 2),
        "paired_logloss_gain": round(md, 5), "paired_t": round(t, 2),
        "blend_best_alpha": best_a, "blend_best_logloss": round(best_ll, 5),
        "blend_alpha0_logloss": round(grid[0][1], 5), "blend_alpha1_logloss": round(grid[-1][1], 5),
        "blend_grid": [{"alpha": a, "logloss": round(v, 5)} for a, v in grid],
        "model_calibration": cal,
    }


def build_hints(ticket, race):
    h = []

    def add(level, text):
        h.append({"level": level, "text": text})

    ov = (ticket or {}).get("overall") or {}
    n = ov.get("n", 0)
    if n and n < 300:
        add("注意", f"買い目単位の評価件数が{n}件と少なく、結論はぶれやすい状態です(目安: 的中200件以上)。")
    ratio = ov.get("calibration_ratio")
    if n >= 300 and ratio is not None:
        if ratio < 0.85:
            add("要改善", f"予想勝率は全体で過大評価です(実的中/予想={ratio})。勝率を約{round(ratio*100)}%に縮める補正が必要です。")
        elif ratio > 1.15:
            add("要改善", f"予想勝率は全体で過小評価です(実的中/予想={ratio})。")
        else:
            add("良好", f"予想勝率は全体として実績と概ね一致しています(実的中/予想={ratio})。")
    bss = ov.get("brier_skill_vs_const")
    if bss is not None and n >= 300:
        add("参考", f"Brierスキル(定数予想比)={bss}。0以下なら、予想勝率は『全部同じ確率』と変わらず識別力がありません。")
    bad = [b for b in (ticket or {}).get("by_pred_band", []) if b["verdict"] in ("過大評価", "過小評価")]
    for b in bad[:4]:
        add("要改善", f"予想{b['band']}帯: {b['verdict']}(予想{b['mean_pred_pct']}%→実績{b['hit_pct']}%, n={b['n']})。補正係数の目安は{b['suggested_factor']}です。")
    for b in (ticket or {}).get("by_ev_band", []):
        if b["n"] >= 30 and b.get("expected_roi_pct") and b.get("roi_pct") is not None and b["roi_pct"] < 0.6 * b["expected_roi_pct"]:
            add("要改善", f"EV{b['band']}帯は想定回収率{b['expected_roi_pct']}%に対し実績{b['roi_pct']}%(n={b['n']})。この帯のEVは過大です。")
    if race and race.get("n", 0) >= 50:
        g, t = race["paired_logloss_gain"], race["paired_t"]
        if t >= 2:
            add("良好", f"1着確率の精度でAIは市場(オッズ)より有意に良好です(logloss差={g}, t={t}, n={race['n']}レース)。")
        elif t <= -2:
            add("要改善", f"1着確率の精度でAIは市場(オッズ)より有意に悪いです(logloss差={g}, t={t})。AI単独の確率はオッズより信頼できません。")
        else:
            add("参考", f"AIと市場の1着確率精度に有意差はありません(logloss差={g}, t={t}, n={race['n']}レース)。")
        a = race["blend_best_alpha"]
        gain_blend = min(race["blend_alpha0_logloss"], race["blend_alpha1_logloss"]) - race["blend_best_logloss"]
        if 0 < a < 1 and gain_blend < 0.001:
            add("参考", "AI確率と市場確率を混ぜても精度はほぼ変わりません(改善幅が0.001未満)。")
        elif 0 < a < 1:
            add("改善案", f"AI確率と市場確率を混ぜると精度が上がります(最良のAI重み={a}、logloss {race['blend_alpha0_logloss']}→{race['blend_best_logloss']})。AI確率にオッズ由来の確率を{round((1-a)*100)}%混ぜる案を検討できます。")
        elif a == 0:
            add("改善案", "AIの重みを0にした(市場確率のみ)のが最良でした。現状のAI確率は市場に付加価値を与えていません。")
        elif a == 1:
            add("参考", "AI単独が最良でした。市場確率を混ぜる必要はありません。")
        if race["top1_model_pct"] < race["top1_market_pct"]:
            add("参考", f"1着的中率(本命一致)はAI {race['top1_model_pct']}% < 市場 {race['top1_market_pct']}%。")
    elif race is not None:
        add("注意", "AIと市場の比較に使えるレースが50件未満です(結果確定済み・AI予想あり・3連単オッズありのレースが必要)。過去レース収集が進むと精度が上がります。")
    if not h:
        add("注意", "評価できるデータがまだありません。")
    return h
