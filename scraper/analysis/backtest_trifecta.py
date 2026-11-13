"""3連単 EV backtest。
- モデル確率 P(a,b,c) を MLP で計算
- odds['3連単']['matrix'] を race_id 経由で JSON から読む
- 買い目: P >= 0.02 かつ EV = P*odds - 1 > threshold
- 1点 100円固定、58点グリッドで ROI 評価
"""
import numpy as np, json, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)
from trifecta_roi import load_inference_data, predict_race_probs_mixed


def load_odds_for_race(race_id):
    """race_id = 'oddspark:<jo>:<kaisai>:<rno>' から JSON を読んで odds dict を返す。"""
    parts = race_id.split(':')
    if len(parts) != 4: return None
    _, jo, kai, rno = parts
    # scraper/data*/20260101_24_01.json 形式
    pat = f"scraper/data*/{kai}_{jo}_{int(rno):02d}.json"
    import glob
    cand = sorted(glob.glob(pat))
    if not cand: return None
    try:
        d = json.load(open(cand[0], encoding='utf-8'))
    except Exception:
        return None
    mat = d.get('odds', {}).get('3連単', {}).get('matrix')
    if not mat: return None
    odds = {}
    for row in mat:
        try:
            a = int(row['1着']); b = int(row['2着']); c = int(row['3着'])
            o = float(str(row['オッズ']).replace(',', ''))
            odds[(a, b, c)] = o
        except (KeyError, ValueError):
            continue
    return odds


def backtest(thresholds):
    data = load_inference_data('scraper/analysis/data/test.npz', 'scraper/analysis/data/norm.npz')
    W1 = dict(np.load('scraper/analysis/data/W_1st_mlp.npz'))
    W2 = dict(np.load('scraper/analysis/data/W_2nd_mlp.npz'))
    W3 = dict(np.load('scraper/analysis/data/W_3rd_mlp.npz'))
    N = len(data['y'])
    # 集計用: 閾値ごとの (cost, payout, hits, n_bets)
    res = {th: {'cost': 0, 'payout': 0, 'hits': 0, 'n_bets': 0} for th in thresholds}

    missing_odds = 0
    for i in range(N):
        race_id = str(data['race_id'][i]) if 'race_id' in data else None
        odds_dict = load_odds_for_race(race_id) if race_id else None
        if odds_dict is None:
            missing_odds += 1
            continue
        probs = predict_race_probs_mixed(
            data['X'][i], data['mask'][i], data['scores'][i],
            data['line_ids'][i], data['line_pos'][i], W1, W2, W3)
        if not probs: continue
        cars = data['cars'][i]
        pc = {(int(cars[a]), int(cars[b]), int(cars[c])): p for (a,b,c), p in probs.items()}
        a, b, c = map(int, data['y'][i])
        actual = (int(cars[a]), int(cars[b]), int(cars[c]))
        for th in thresholds:
            for combo, p in pc.items():
                if p < 0.02: continue
                o = odds_dict.get(combo)
                if o is None or o <= 0: continue
                ev = p * o
                if ev <= th: continue
                res[th]['cost'] += 100
                res[th]['n_bets'] += 1
                if combo == actual:
                    res[th]['payout'] += int(round(100 * o))
                    res[th]['hits'] += 1
    print(f'missing_odds={missing_odds} / N={N}')
    print(f"{'th':>5} {'n_bets':>8} {'hits':>5} {'cost':>10} {'payout':>10} {'ROI%':>7} {'hit_rate%':>10}")
    for th in thresholds:
        r = res[th]
        roi = (r['payout']/r['cost']*100) if r['cost'] > 0 else 0
        hr = (r['hits']/r['n_bets']*100) if r['n_bets'] > 0 else 0
        print(f"{th:>5.2f} {r['n_bets']:>8} {r['hits']:>5} {r['cost']:>10} {r['payout']:>10} {roi:>7.1f} {hr:>10.3f}")


if __name__ == '__main__':
    ths = [1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30, 1.40, 1.50, 1.75, 2.00, 2.50, 3.00, 4.00, 5.00]
    backtest(ths)
