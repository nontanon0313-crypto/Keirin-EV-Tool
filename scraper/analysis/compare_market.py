"""モデル確率 P vs 市場インプライド確率 の直接比較。
- 全3連単組合せを対象、フィルタなし
- 市場確率 = (1/odds) を全組合せ合計で正規化
"""
import numpy as np, json, glob, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE); sys.path.insert(0, _ROOT)
from trifecta_roi import load_inference_data, predict_race_probs_mixed


def load_odds_and_implied(race_id):
    parts = race_id.split(':')
    if len(parts) != 4: return None
    _, jo, kai, rno = parts
    pat = f"scraper/data*/{kai}_{jo}_{int(rno):02d}.json"
    cand = sorted(glob.glob(pat))
    if not cand: return None
    try:
        d = json.load(open(cand[0], encoding='utf-8'))
    except Exception:
        return None
    mat = d.get('odds', {}).get('3連単', {}).get('matrix')
    if not mat: return None
    raw = {}
    for row in mat:
        try:
            a=int(row['1着']); b=int(row['2着']); c=int(row['3着'])
            o=float(str(row['オッズ']).replace(',',''))
            if o <= 0: continue
            raw[(a,b,c)] = 1.0/o
        except (KeyError, ValueError):
            continue
    if not raw: return None
    s = sum(raw.values())
    if s <= 0: return None
    implied = {k: v/s for k, v in raw.items()}
    takeout = 1 - 1.0/s
    return implied, takeout


def main():
    data = load_inference_data('scraper/analysis/data/test.npz','scraper/analysis/data/norm.npz')
    W1 = dict(np.load('scraper/analysis/data/W_1st_mlp.npz'))
    W2 = dict(np.load('scraper/analysis/data/W_2nd_mlp.npz'))
    W3 = dict(np.load('scraper/analysis/data/W_3rd_mlp.npz'))
    N = len(data['y'])

    ll_model = []; ll_market = []
    brier_model = []; brier_market = []
    hit_m_model = 0; hit_m_market = 0
    rank_model = []; rank_market = []
    takeouts = []
    n_used = 0

    for i in range(N):
        race_id = str(data['race_id'][i]) if 'race_id' in data else None
        if not race_id: continue
        res = load_odds_and_implied(race_id)
        if res is None: continue
        implied, to = res
        takeouts.append(to)

        probs = predict_race_probs_mixed(
            data['X'][i], data['mask'][i], data['scores'][i],
            data['line_ids'][i], data['line_pos'][i], W1, W2, W3)
        if not probs: continue
        cars = data['cars'][i]
        pc = {(int(cars[a]),int(cars[b]),int(cars[c])): p for (a,b,c),p in probs.items()}

        common = set(pc.keys()) & set(implied.keys())
        if len(common) < 10: continue
        a,b,c = map(int, data['y'][i])
        actual = (int(cars[a]),int(cars[b]),int(cars[c]))
        if actual not in common: continue

        # 再正規化（共通キー内で確率和を1に）
        sp = sum(pc[k] for k in common)
        sm = sum(implied[k] for k in common)
        if sp <= 0 or sm <= 0: continue
        p_model = {k: pc[k]/sp for k in common}
        p_market = {k: implied[k]/sm for k in common}

        # logloss
        ll_model.append(-np.log(p_model[actual] + 1e-12))
        ll_market.append(-np.log(p_market[actual] + 1e-12))

        # brier (multiclass, sum over combos)
        bm = sum((p_model[k] - (1.0 if k==actual else 0.0))**2 for k in common)
        bk = sum((p_market[k] - (1.0 if k==actual else 0.0))**2 for k in common)
        brier_model.append(bm)
        brier_market.append(bk)

        # top1
        top_model = max(p_model.items(), key=lambda x: x[1])[0]
        top_market = max(p_market.items(), key=lambda x: x[1])[0]
        if top_model == actual: hit_m_model += 1
        if top_market == actual: hit_m_market += 1

        # rank of actual
        sorted_model = sorted(p_model.items(), key=lambda x: -x[1])
        sorted_market = sorted(p_market.items(), key=lambda x: -x[1])
        rank_model.append(next(j for j,(k,_) in enumerate(sorted_model) if k==actual) + 1)
        rank_market.append(next(j for j,(k,_) in enumerate(sorted_market) if k==actual) + 1)

        n_used += 1
        if n_used % 200 == 0:
            print(f'  processed {n_used}')

    print(f'\n=== N_used = {n_used} / {N} ===')
    print(f'平均控除率 (1 - 1/sum(1/odds)): {np.mean(takeouts)*100:.2f}%')
    print()
    print(f'【対数損失】(lower is better)')
    print(f'  model  : {np.mean(ll_model):.4f}')
    print(f'  market : {np.mean(ll_market):.4f}')
    print(f'  diff   : {np.mean(ll_model) - np.mean(ll_market):+.4f}  (負なら model 優位)')
    print()
    print(f'【ブライア・スコア】(lower is better)')
    print(f'  model  : {np.mean(brier_model):.5f}')
    print(f'  market : {np.mean(brier_market):.5f}')
    print(f'  diff   : {np.mean(brier_model) - np.mean(brier_market):+.5f}  (負なら model 優位)')
    print()
    print(f'【本命一致率】(argmax が実績と一致)')
    print(f'  model  : {hit_m_model/n_used*100:.3f}% ({hit_m_model}/{n_used})')
    print(f'  market : {hit_m_market/n_used*100:.3f}% ({hit_m_market}/{n_used})')
    print()
    print(f'【実績組合せのランク】(中央値 / 平均)')
    print(f'  model  : median={np.median(rank_model):.0f}  mean={np.mean(rank_model):.1f}')
    print(f'  market : median={np.median(rank_market):.0f}  mean={np.mean(rank_market):.1f}')


if __name__ == '__main__':
    main()
