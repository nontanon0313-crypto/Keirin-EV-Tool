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

def process_race_v2(path):
    res = extract_one_race(path)
    if not res: return None
    riders, line_map, pos_map, d = res
    if len(riders) != 7: return None
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
        X = np.array([rider_features(r, line_map, pos_map, scores, venue_name=venue) for r in riders], dtype=np.float32)
    except Exception:
        return None
    if X.shape != (7, 27): return None
    car_to_idx = {int(r['車番']): i for i, r in enumerate(riders)}
    try:
        y1, y2, y3 = [car_to_idx[c] for c in order]
    except KeyError:
        return None
    return X, y1, y2, y3, f"oddspark:{jo}:{kai}:{rno}"

def main():
    files = sorted(glob.glob('scraper/data*/2026*.json'))
    print(f"対象ファイル: {len(files)}")
    all_X, all_y = [], []
    n_err = 0
    for i, f in enumerate(files):
        r = process_race_v2(f)
        if r is None:
            n_err += 1
            continue
        X, y1, y2, y3, rid = r
        all_X.append(X); all_y.append((y1, y2, y3))
        if (i+1) % 2000 == 0:
            print(f"  {i+1}/{len(files)}  成功{len(all_X)}  失敗{n_err}")
    if not all_X:
        print("ERROR: 有効データが空")
        return
    all_X = np.array(all_X, dtype=np.float32)
    all_y = np.array(all_y, dtype=np.int32)
    n_total = len(all_X)
    rng = np.random.default_rng(42)
    perm = rng.permutation(n_total)
    n_train = int(n_total * 0.8)
    train_idx = perm[:n_train]
    test_idx = perm[n_train:]
    train_X, train_y = all_X[train_idx], all_y[train_idx]
    test_X, test_y = all_X[test_idx], all_y[test_idx]
    if len(train_X) == 0 or len(test_X) == 0:
        print("ERROR: 訓練/検証データが空")
        return
    train_X = np.array(train_X, dtype=np.float32)
    test_X = np.array(test_X, dtype=np.float32)
    train_y = np.array(train_y, dtype=np.int32)
    test_y = np.array(test_y, dtype=np.int32)
    out_dir = 'scraper/analysis/data'
    os.makedirs(out_dir, exist_ok=True)
    np.savez_compressed(f'{out_dir}/train.npz', X=train_X, y=train_y)
    np.savez_compressed(f'{out_dir}/test.npz', X=test_X, y=test_y)
    print(f"\n訓練: X{train_X.shape} y{train_y.shape}")
    print(f"検証: X{test_X.shape} y{test_y.shape}")
    print(f"保存: {out_dir}/train.npz, test.npz")

if __name__ == '__main__':
    main()
