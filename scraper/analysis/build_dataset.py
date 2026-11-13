import json, glob, os, sys
import numpy as np
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)
from extract_features import extract_one_race, rider_features, _race_ctx
from venue_region import venue_region
from app.keirin_data import JO_CODE_TO_VENUE

MAX_CARS = 9


def jo_to_venue(jo_code):
    return JO_CODE_TO_VENUE.get(str(jo_code))


def _safe_scores(riders):
    out = []
    for r in riders:
        try:
            out.append(float(r['競走得点']))
        except Exception:
            out.append(0.0)
    return out


def _line_arrays(riders, line_map, pos_map):
    cars = np.zeros(MAX_CARS, dtype=np.int32)
    line_ids = np.full(MAX_CARS, -1, dtype=np.int32)
    line_pos = np.full(MAX_CARS, -1, dtype=np.int32)
    for i, r in enumerate(riders):
        car = int(r['車番'])
        cars[i] = car
        line_ids[i] = line_map.get(car, -1)
        line_pos[i] = pos_map.get(car, -1)
    return cars, line_ids, line_pos


def build_X2(X, scores, line_ids, line_pos, mask, a_slot):
    """2着モデル入力（9スロット固定、30次元基本 + 関係4 = 34次元）。
    a_slot = 1着車のスロット番号（実績でも予測でもよい）。
    X2[a_slot] は mask2=0、それ以外の有効スロットは mask2=1。
    """
    X2 = np.zeros((MAX_CARS, 34), dtype=np.float32)
    mask2 = np.zeros(MAX_CARS, dtype=np.float32)
    if mask[a_slot] <= 0:
        return X2, mask2
    a_line = int(line_ids[a_slot])
    a_score = float(scores[a_slot])
    a_pos = int(line_pos[a_slot])
    for i in range(MAX_CARS):
        if mask[i] <= 0 or i == a_slot:
            continue
        sl = 1.0 if (int(line_ids[i]) == a_line and a_line >= 0) else 0.0
        pd = float(int(line_pos[i]) - a_pos) if (a_pos >= 0 and int(line_pos[i]) >= 0) else 0.0
        sd = float(scores[i] - a_score)
        X2[i, :30] = X[i, :30]
        X2[i, 30] = sl
        X2[i, 31] = pd
        X2[i, 32] = sd
        X2[i, 33] = float(a_line)
        mask2[i] = 1.0
    return X2, mask2


def build_X3(X, scores, line_ids, line_pos, mask, a_slot, b_slot):
    """3着モデル入力（9スロット固定、30次元基本 + 関係8 = 38次元）。
    a_slot = 1着車, b_slot = 2着車 のスロット番号（実績でも予測でもよい）。
    """
    X3 = np.zeros((MAX_CARS, 38), dtype=np.float32)
    mask3 = np.zeros(MAX_CARS, dtype=np.float32)
    if mask[a_slot] <= 0 or mask[b_slot] <= 0:
        return X3, mask3
    a_line = int(line_ids[a_slot]); a_score = float(scores[a_slot]); a_pos = int(line_pos[a_slot])
    b_line = int(line_ids[b_slot]); b_score = float(scores[b_slot]); b_pos = int(line_pos[b_slot])
    for i in range(MAX_CARS):
        if mask[i] <= 0 or i == a_slot or i == b_slot:
            continue
        sl1 = 1.0 if (int(line_ids[i]) == a_line and a_line >= 0) else 0.0
        pd1 = float(int(line_pos[i]) - a_pos) if (a_pos >= 0 and int(line_pos[i]) >= 0) else 0.0
        sd1 = float(scores[i] - a_score)
        sl2 = 1.0 if (int(line_ids[i]) == b_line and b_line >= 0) else 0.0
        pd2 = float(int(line_pos[i]) - b_pos) if (b_pos >= 0 and int(line_pos[i]) >= 0) else 0.0
        sd2 = float(scores[i] - b_score)
        X3[i, :30] = X[i, :30]
        X3[i, 30] = sl1; X3[i, 31] = pd1; X3[i, 32] = sd1; X3[i, 33] = float(a_line)
        X3[i, 34] = sl2; X3[i, 35] = pd2; X3[i, 36] = sd2; X3[i, 37] = float(b_line)
        mask3[i] = 1.0
    return X3, mask3


def process_race(path):
    """旧API（互換用、7車固定）。"""
    res = extract_one_race(path)
    if not res:
        return None
    riders, line_map, pos_map, d = res
    jo = d.get('jo_code', '')
    kai = d.get('kaisai_bi', '')
    rno = d.get('race_no', 0)
    venue = venue_region(jo)
    results = d.get('result', {}).get('results', [])
    if not results or len(results) < 3:
        return None
    order = [int(r['車番']) for r in sorted(results, key=lambda x: int(x['着順']))[:3]]
    scores = _safe_scores(riders)
    try:
        X = np.array([rider_features(r, line_map, pos_map, scores, venue_name=venue) for r in riders], dtype=np.float32)
    except Exception:
        return None
    if X.shape != (7, 27):
        return None
    car_to_idx = {int(r['車番']): i for i, r in enumerate(riders)}
    try:
        y1, y2, y3 = [car_to_idx[c] for c in order]
    except KeyError:
        return None
    return X, y1, y2, y3, f"oddspark:{jo}:{kai}:{rno}"


def process_race_v2(path):
    res = extract_one_race(path)
    if not res:
        return None
    riders, line_map, pos_map, d = res
    n = len(riders)
    if not (5 <= n <= MAX_CARS):
        return None
    jo = str(d.get('jo_code', ''))
    kai = str(d.get('kaisai_bi', ''))
    rno = d.get('race_no', 0)
    venue = jo_to_venue(jo)
    if not venue:
        return None
    results = d.get('result', {}).get('results', [])
    if not results or len(results) < 3:
        return None
    order = [int(r['車番']) for r in sorted(results, key=lambda x: int(x['着順']))[:3]]
    scores_list = _safe_scores(riders)
    race_ctx = _race_ctx(d)
    race_ctx["score_by_car"] = {int(r['車番']): s for r, s in zip(riders, scores_list)}
    try:
        feats = np.array([rider_features(r, line_map, pos_map, scores_list, venue_name=venue, race_ctx=race_ctx) for r in riders], dtype=np.float32)
    except Exception:
        return None
    if feats.shape != (n, 30):
        return None

    X = np.zeros((MAX_CARS, 30), dtype=np.float32)
    mask = np.zeros(MAX_CARS, dtype=np.float32)
    X[:n] = feats
    mask[:n] = 1.0

    cars, line_ids, line_pos = _line_arrays(riders, line_map, pos_map)
    scores = np.zeros(MAX_CARS, dtype=np.float32)
    scores[:n] = scores_list

    car_to_idx = {int(r['車番']): i for i, r in enumerate(riders)}
    try:
        y1, y2, y3 = [car_to_idx[c] for c in order]
    except KeyError:
        return None

    X2, mask2 = build_X2(X, scores, line_ids, line_pos, mask, y1)
    X3, mask3 = build_X3(X, scores, line_ids, line_pos, mask, y1, y2)

    return {
        'X': X, 'mask': mask, 'n': n, 'y': (y1, y2, y3),
        'X2': X2, 'mask2': mask2, 'y2': y2,
        'X3': X3, 'mask3': mask3, 'y3': y3,
        'cars': cars, 'scores': scores,
        'line_ids': line_ids, 'line_pos': line_pos,
        'race_id': f"oddspark:{jo}:{kai}:{rno}",
    }


def main():
    files = sorted(glob.glob('scraper/data*/2026*.json'))
    print(f"対象ファイル: {len(files)}")
    keys_arr = ['X', 'mask', 'n', 'X2', 'mask2', 'y2', 'X3', 'mask3', 'y3',
                'cars', 'scores', 'line_ids', 'line_pos']
    all_d = {k: [] for k in keys_arr}
    all_y, all_race_ids = [], []
    n_err = 0
    for i, f in enumerate(files):
        r = process_race_v2(f)
        if r is None:
            n_err += 1
            continue
        for k in keys_arr:
            all_d[k].append(r[k])
        all_y.append(r['y'])
        all_race_ids.append(r['race_id'])
        if (i + 1) % 2000 == 0:
            print(f"  {i+1}/{len(files)}  成功{len(all_y)}  失敗{n_err}")
    if not all_y:
        print("ERROR: 有効データが空")
        return
    arrays = {k: np.array(v) for k, v in all_d.items()}
    arrays['y'] = np.array(all_y, dtype=np.int32)
    arrays['mask'] = arrays['mask'].astype(np.float32)
    arrays['mask2'] = arrays['mask2'].astype(np.float32)
    arrays['mask3'] = arrays['mask3'].astype(np.float32)
    arrays['n'] = arrays['n'].astype(np.int32)
    arrays['y2'] = arrays['y2'].astype(np.int32)
    arrays['y3'] = arrays['y3'].astype(np.int32)
    arrays['cars'] = arrays['cars'].astype(np.int32)
    arrays['scores'] = arrays['scores'].astype(np.float32)
    arrays['line_ids'] = arrays['line_ids'].astype(np.int32)
    arrays['line_pos'] = arrays['line_pos'].astype(np.int32)
    arrays['race_id'] = np.array(all_race_ids, dtype='U40')

    n_total = len(arrays['y'])
    rng = np.random.default_rng(42)
    perm = rng.permutation(n_total)
    n_train = int(n_total * 0.8)
    tr_idx = perm[:n_train]
    te_idx = perm[n_train:]

    out_dir = 'scraper/analysis/data'
    os.makedirs(out_dir, exist_ok=True)
    tr = {k: arrays[k][tr_idx] for k in arrays}
    te = {k: arrays[k][te_idx] for k in arrays}
    np.savez_compressed(f'{out_dir}/train.npz', **tr)
    np.savez_compressed(f'{out_dir}/test.npz', **te)
    print(f"\n訓練: X{tr['X'].shape} y{tr['y'].shape}")
    print(f"検証: X{te['X'].shape} y{te['y'].shape}")
    print(f"保存: {out_dir}/train.npz, test.npz")


if __name__ == '__main__':
    main()
