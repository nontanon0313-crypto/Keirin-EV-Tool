import json, glob, os, sys
import numpy as np
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)
from extract_features import extract_one_race, rider_features
from venue_region import venue_region

def process_race(path):
    """1レースを処理。成功: (X_7x27, y_1着idx, y_2着idx, y_3着idx, race_id) 失敗: None"""
    res = extract_one_race(path)
    if not res: return None
    riders, line_map, pos_map, d = res
    jo = d.get('jo_code', '')
    kai = d.get('kaisai_bi', '')
    rno = d.get('race_no', 0)
    venue = venue_region(jo)  # 会場名は別途、jo_code→venueに置換要
    results = d.get('result', {}).get('results', [])
    if not results or len(results) < 3: return None
    order = [int(r['車番']) for r in sorted(results, key=lambda x: int(x['着順']))[:3]]
    scores = []
    for r in riders:
        try: scores.append(float(r['競走得点']))
        except: scores.append(0.0)
    try:
        X = np.array([rider_features(r, line_map, pos_map, scores, venue_name=venue) for r in riders], dtype=np.float32)
    except Exception as e:
        return None
    if X.shape != (7, 27): return None
    car_to_idx = {int(r['車番']): i for i, r in enumerate(riders)}
    try:
        y1, y2, y3 = [car_to_idx[c] for c in order]
    except KeyError:
        return None
    return X, y1, y2, y3, f"oddspark:{jo}:{kai}:{rno}"

from app.keirin_data import JO_CODE_TO_VENUE

def jo_to_venue(jo_code):
    return JO_CODE_TO_VENUE.get(str(jo_code))

MAX_CARS = 9

def process_race_v2(path):
    res = extract_one_race(path)
    if not res: return None
    riders, line_map, pos_map, d = res
    if not (5 <= len(riders) <= MAX_CARS): return None
    jo = str(d.get('jo_code', ''))
    kai = str(d.get('kaisai_bi', ''))
    rno = d.get('race_no', 0)
    venue = jo_to_venue(jo)
    if not venue: return None
    results = d.get('result', {}).get('results', [])
    if not results or len(results) < 3: return None
    order = [int(r['車番']) for r in sorted(results, key=lambda x: int(x['着順']))[:3]]
    scores = []
    for r in riders:
        try: scores.append(float(r['競走得点']))
        except: scores.append(0.0)
    try:
        feats = np.array([rider_features(r, line_map, pos_map, scores, venue_name=venue) for r in riders], dtype=np.float32)
    except Exception:
        return None
    if feats.shape != (len(riders), 27): return None
    n = len(riders)
    X = np.zeros((MAX_CARS, 27), dtype=np.float32)
    mask = np.zeros(MAX_CARS, dtype=np.float32)
    X[:n] = feats
    mask[:n] = 1.0
    car_to_idx = {int(r['車番']): i for i, r in enumerate(riders)}
    try:
        y1, y2, y3 = [car_to_idx[c] for c in order]
    except KeyError:
        return None
    # 2着用データ: 1着を除いた8車、+1着との関係特徴4つ
    X2 = np.zeros((MAX_CARS, 31), dtype=np.float32)
    mask2 = np.zeros(MAX_CARS, dtype=np.float32)
    win_car = int(riders[y1]['車番'])
    win_line = line_map.get(win_car, -1)
    win_score = scores[y1]
    win_pos = pos_map.get(win_car, -1)
    j = 0
    y2_relidx = -1
    for i, r in enumerate(riders):
        if i == y1: continue
        car = int(r['車番'])
        same_line = 1.0 if line_map.get(car) == win_line and win_line >= 0 else 0.0
        pos_diff = (pos_map.get(car, -1) - win_pos) if win_pos >= 0 and pos_map.get(car) is not None else 0.0
        score_diff = scores[i] - win_score
        X2[j, :27] = X[i, :27]
        X2[j, 27] = same_line
        X2[j, 28] = pos_diff
        X2[j, 29] = score_diff
        X2[j, 30] = win_line  # 1着のラインID
        mask2[j] = 1.0
        if i == y2: y2_relidx = j
        j += 1
    return X, mask, y1, y2, y3, X2, mask2, y2_relidx, n, f"oddspark:{jo}:{kai}:{rno}"

def main():
    files = sorted(glob.glob('scraper/data*/2026*.json'))
    print(f"対象ファイル: {len(files)}")
    all_X, all_mask, all_n, all_y = [], [], [], []
    all_X2, all_mask2, all_y2 = [], [], []
    n_err = 0
    for i, f in enumerate(files):
        r = process_race_v2(f)
        if r is None:
            n_err += 1
            continue
        X, mask, y1, y2, y3, X2, mask2, y2_relidx, n, rid = r
        all_X.append(X); all_mask.append(mask); all_n.append(n); all_y.append((y1, y2, y3))
        all_X2.append(X2); all_mask2.append(mask2); all_y2.append(y2_relidx)
        if (i+1) % 2000 == 0:
            print(f"  {i+1}/{len(files)}  成功{len(all_X)}  失敗{n_err}")
    if not all_X:
        print("ERROR: 有効データが空")
        return
    all_X = np.array(all_X, dtype=np.float32)
    all_mask = np.array(all_mask, dtype=np.float32)
    all_n = np.array(all_n, dtype=np.int32)
    all_y = np.array(all_y, dtype=np.int32)
    all_X2 = np.array(all_X2, dtype=np.float32)
    all_mask2 = np.array(all_mask2, dtype=np.float32)
    all_y2 = np.array(all_y2, dtype=np.int32)
    n_total = len(all_X)
    rng = np.random.default_rng(42)
    perm = rng.permutation(n_total)
    n_train = int(n_total * 0.8)
    train_idx = perm[:n_train]
    test_idx = perm[n_train:]
    train_X, train_mask, train_n, train_y = all_X[train_idx], all_mask[train_idx], all_n[train_idx], all_y[train_idx]
    test_X, test_mask, test_n, test_y = all_X[test_idx], all_mask[test_idx], all_n[test_idx], all_y[test_idx]
    train_X2, train_mask2, train_y2 = all_X2[train_idx], all_mask2[train_idx], all_y2[train_idx]
    test_X2, test_mask2, test_y2 = all_X2[test_idx], all_mask2[test_idx], all_y2[test_idx]
    if len(train_X) == 0 or len(test_X) == 0:
        print("ERROR: 訓練/検証データが空")
        return
    train_X = np.array(train_X, dtype=np.float32)
    test_X = np.array(test_X, dtype=np.float32)
    train_y = np.array(train_y, dtype=np.int32)
    test_y = np.array(test_y, dtype=np.int32)
    out_dir = 'scraper/analysis/data'
    os.makedirs(out_dir, exist_ok=True)
    np.savez_compressed(f'{out_dir}/train.npz', X=train_X, mask=train_mask, n=train_n, y=train_y, X2=train_X2, mask2=train_mask2, y2=train_y2)
    np.savez_compressed(f'{out_dir}/test.npz', X=test_X, mask=test_mask, n=test_n, y=test_y, X2=test_X2, mask2=test_mask2, y2=test_y2)
    print(f"\n訓練: X{train_X.shape} y{train_y.shape}")
    print(f"検証: X{test_X.shape} y{test_y.shape}")
    print(f"保存: {out_dir}/train.npz, test.npz")

if __name__ == '__main__':
    main()
