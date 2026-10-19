"""モデル評価の純粋計算(DB非依存・単体テスト可能)。

ticket_eval : 買い目単位(実購入+見送り評価)の予想勝率の当たり具合
race_eval   : レース単位でAIの1着確率と市場(3連単オッズ)の1着確率を比較
build_hints : 結果から改善の手がかりを日本語で列挙
"""
import math
import random

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


def _ticket_eval_rows(rows):
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


def _roi_group(rows, boots=1000, seed=7):
    """rows: (cluster, stake, payout, win)。clusterはレース。レース単位ブートストラップでROIの幅を出す。"""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    stake = sum(r[1] for r in rows)
    payout = sum(r[2] for r in rows)
    wins = sorted((r[2] for r in rows if r[3]), reverse=True)
    out = {
        "n": n, "wins": len(wins), "stake": round(stake), "payout": round(payout),
        "roi_pct": round(payout / stake * 100, 1) if stake else None,
    }
    for k in (1, 3, 5):
        if wins and stake:
            out[f"roi_excl_top{k}_pct"] = round((payout - sum(wins[:k])) / stake * 100, 1)
    out["top1_share_pct"] = round(wins[0] / payout * 100, 1) if wins and payout else None
    # 市場が正しい(控除率25%)場合の期待的中数 = Σ 0.75/オッズ。実際の的中がこの何倍かを見る
    exp_hits = sum(0.75 / r[4] for r in rows if len(r) > 4 and r[4])
    if exp_hits > 0:
        out["market_expected_hits"] = round(exp_hits, 1)
        out["hits_vs_market_x"] = round(len(wins) / exp_hits, 1)
    # レース単位ブートストラップ
    by = {}
    for c, st, po, w, *_ in rows:
        a = by.setdefault(c, [0.0, 0.0])
        a[0] += st
        a[1] += po
    cl = list(by.values())
    if len(cl) >= 20 and stake:
        rnd = random.Random(seed)
        m = len(cl)
        rois = []
        for _ in range(boots):
            st = po = 0.0
            for _ in range(m):
                a = cl[rnd.randrange(m)]
                st += a[0]
                po += a[1]
            if st > 0:
                rois.append(po / st * 100)
        rois.sort()
        if rois:
            out["boot_roi_p05"] = round(rois[int(len(rois) * 0.05)], 1)
            out["boot_roi_p50"] = round(rois[int(len(rois) * 0.50)], 1)
            out["boot_roi_p95"] = round(rois[min(len(rois) - 1, int(len(rois) * 0.95))], 1)
            out["boot_p_roi_over_100_pct"] = round(sum(1 for x in rois if x > 100) / len(rois) * 100, 1)
    return out


def roi_report(rows, min_p=0.02, min_odds=125.0):
    """rows: (cluster, stake, payout, win, timing, p, odds)  timing: live | replay | unknown
    live=発走前に記録(実運用)、replay=結果確定後の再投票(バックテスト)。"""
    def sel(f):
        return [(r[0], r[1], r[2], r[3], r[6]) for r in rows if f(r)]
    cond = lambda r: r[5] is not None and r[6] is not None and r[5] >= min_p and r[6] >= min_odds
    groups = {
        "全購入": sel(lambda r: True),
        "全購入・実運用(発走前)": sel(lambda r: r[4] == "live"),
        "全購入・再投票(結果確定後)": sel(lambda r: r[4] == "replay"),
        f"運用条件(予想{min_p*100:g}%以上かつ{min_odds:g}倍以上)": sel(cond),
        "運用条件・実運用(発走前)": sel(lambda r: cond(r) and r[4] == "live"),
        "運用条件・再投票(結果確定後)": sel(lambda r: cond(r) and r[4] == "replay"),
        "運用条件・AI予想が発走前のレース": sel(lambda r: cond(r) and len(r) > 7 and r[7] == "pre"),
        "運用条件・AI予想が発走後のレース": sel(lambda r: cond(r) and len(r) > 7 and r[7] == "post"),
    }
    return {k: _roi_group(v) for k, v in groups.items()}


def roi_hints(rep):
    h = []
    live = rep.get("運用条件・実運用(発走前)", {})
    rpl = rep.get("運用条件・再投票(結果確定後)", {})
    allc = [v for k, v in rep.items() if k.startswith("運用条件(")]
    allc = allc[0] if allc else {}
    if live.get("n", 0) and live.get("wins", 0) < 30:
        h.append({"level": "注意", "text": f"実運用(発走前)の運用条件は的中{live.get('wins', 0)}件のみで、収益構造の検証には不足です(目安: 的中100件以上)。"})
    if live.get("roi_pct") is not None and rpl.get("roi_pct") is not None and rpl["roi_pct"] > 2 * max(live["roi_pct"], 1):
        h.append({"level": "要改善", "text": f"再投票(バックテスト)の回収率{rpl['roi_pct']}%に対し、実運用は{live['roi_pct']}%です。再投票の数字は楽観的で、実運用の根拠にできません。"})
    if allc.get("boot_roi_p05") is not None:
        if allc["boot_roi_p05"] < 100:
            h.append({"level": "注意", "text": f"運用条件の回収率は、レースを入れ替えて再集計すると下側5%点が{allc['boot_roi_p05']}%(100%未満)です。黒字と断定できる水準ではありません(黒字になる確率{allc['boot_p_roi_over_100_pct']}%)。"})
        else:
            h.append({"level": "良好", "text": f"運用条件の回収率は、下側5%点でも{allc['boot_roi_p05']}%と100%を超えています(黒字になる確率{allc['boot_p_roi_over_100_pct']}%)。"})
    x = allc.get("hits_vs_market_x")
    if x is not None and x >= 3:
        h.append({"level": "要改善", "text": f"運用条件の的中は{allc['wins']}件で、市場が正しい場合の期待({allc.get('market_expected_hits')}件)の{x}倍です。これほどの差は、本当の優位性か、データ・ロジックの不備(結果を知った後の情報の混入など)のどちらかで、AIが1着確率で市場に負けている事実とも整合しません。原因の特定が先です。"})
    pre = rep.get("運用条件・AI予想が発走前のレース", {})
    post = rep.get("運用条件・AI予想が発走後のレース", {})
    if pre.get("n", 0) >= 100 and post.get("n", 0) >= 100:
        if pre.get("hits_vs_market_x") is not None and post.get("hits_vs_market_x") is not None:
            h.append({"level": "参考", "text": f"AI予想が発走前のレース: 市場期待の{pre['hits_vs_market_x']}倍(回収率{pre.get('roi_pct')}%、{pre['n']}件)/ 発走後のレース: {post['hits_vs_market_x']}倍(回収率{post.get('roi_pct')}%、{post['n']}件)。後者だけが突出していれば、結果を知った後の情報が混入した疑いがあります。"})
    elif pre.get("n", 0) < 100:
        h.append({"level": "注意", "text": f"AI予想が発走前だったレースの運用条件の購入は{pre.get('n', 0)}件のみで、発走前と発走後の比較はまだできません。"})
    if allc.get("roi_excl_top3_pct") is not None and allc["roi_excl_top3_pct"] < 100 <= (allc.get("roi_pct") or 0):
        h.append({"level": "要改善", "text": f"上位3的中を除くと回収率が{allc['roi_excl_top3_pct']}%に落ちます。利益が少数の超高配当に依存しています。"})
    if x is not None and x >= 3:
        for it in h:
            if it["text"].startswith("運用条件の回収率は、下側5%点でも"):
                it["level"] = "参考"
                it["text"] += "※市場期待を大きく超えているため、原因の確認が済むまでこの数字は信用しないでください。"
    return h


class TicketAgg:
    """買い目評価のストリーミング集計。見送り(数十万件)を行リストにせず逐次足し込んでメモリを一定に保つ。
    実購入(stake>0)だけは少数なので保持し、EV帯・オッズ帯の集計に使う。"""

    def __init__(self):
        self.n = 0
        self.wins = 0
        self.sp = 0.0
        self.sb = 0.0
        self.sl = 0.0
        self.bands = {}
        self.buy = []

    def add(self, p, y, stake, payout, odds, ev):
        if p is None or not (0 < p <= 1):
            return
        yy = 1 if y else 0
        self.n += 1
        self.wins += yy
        self.sp += p
        self.sb += (p - yy) ** 2
        self.sl += -math.log(max(p, _EPS)) if yy else -math.log(max(1 - p, _EPS))
        b = self.bands.setdefault(_band(P_EDGES, p), [0, 0, 0.0, 0, 0.0, 0.0])
        b[0] += 1
        b[1] += yy
        b[2] += p
        if (stake or 0) > 0:
            b[3] += 1
            b[4] += stake
            b[5] += (payout or 0)
            self.buy.append((p, yy, stake, payout, odds, ev))

    def finalize(self):
        n = self.n
        if n == 0:
            return {"n": 0}
        mean_p = self.sp / n
        hit = self.wins / n
        b0 = hit * (1 - hit)
        ll0 = -(hit * math.log(max(hit, _EPS)) + (1 - hit) * math.log(max(1 - hit, _EPS))) if 0 < hit < 1 else None
        brier = self.sb / n
        overall = {
            "n": n, "wins": self.wins,
            "mean_pred_pct": round(mean_p * 100, 3), "hit_pct": round(hit * 100, 3),
            "calibration_ratio": round(hit / mean_p, 3) if mean_p else None,
            "brier": round(brier, 5),
            "brier_skill_vs_const": round(1 - brier / b0, 4) if b0 > 0 else None,
            "logloss": round(self.sl / n, 5), "logloss_const": round(ll0, 5) if ll0 is not None else None,
        }
        p_bands = []
        for i in sorted(self.bands):
            gn, gw, sp, bn, stake, payout = self.bands[i]
            gp = sp / gn
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
            p_bands.append({
                "band": _label(P_EDGES, i, "%", 100), "n": gn, "wins": gw,
                "mean_pred_pct": round(gp * 100, 3), "hit_pct": round(gw / gn * 100, 3),
                "ratio": round((gw / gn) / gp, 3) if gp else None,
                "ci95_low_pct": round(lo * 100, 3), "ci95_high_pct": round(hi * 100, 3),
                "verdict": verdict,
                "suggested_factor": round(post / gp, 3) if (gp and gn >= 30) else None,
                "buy_n": bn, "roi_pct": round(payout / stake * 100, 1) if stake > 0 else None,
            })
        sub = _ticket_eval_rows(self.buy) if self.buy else {}
        return {"overall": overall, "by_pred_band": p_bands, "purchased": sub.get("purchased"),
                "by_ev_band": sub.get("by_ev_band", []), "by_odds_band": sub.get("by_odds_band", [])}


def ticket_eval(rows):
    a = TicketAgg()
    for r in rows:
        a.add(*r)
    return a.finalize()
