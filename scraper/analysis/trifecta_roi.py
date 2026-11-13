"""3連単EV計算（条件付き連鎖版）。

P(a,b,c) = P1(a) * P2(b|a) * P3(c|a,b)
- a,b,c は車番（cars[slot] で写像）
- P2(b|a) は build_X2(a) を入力した2着モデル
- P3(c|a,b) は build_X3(a,b) を入力した3着モデル
"""
import numpy as np, os, sys, glob
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)
from train_softmax import forward
from train_mlp import forward as forward_mlp
from build_dataset import build_X2, build_X3

MAX_CARS = 9


def load_inference_data(npz_path, norm_path,
                        w1_path=None, w2_path=None, w3_path=None):
    d = np.load(npz_path)
    norm = np.load(norm_path)
    mean, std = norm['mean'], norm['std']
    X = (d['X'] - mean) / std
    X = X * d['mask'][..., None]
    out = {
        'X': X.astype(np.float32),
        'mask': d['mask'].astype(np.float32),
        'n': d['n'].astype(np.int32),
        'y': d['y'].astype(np.int32),
        'cars': d['cars'].astype(np.int32),
        'scores': d['scores'].astype(np.float32),
        'line_ids': d['line_ids'].astype(np.int32),
        'line_pos': d['line_pos'].astype(np.int32),
    }
    if w1_path:
        out['W1'] = np.load(w1_path)
    if w2_path:
        out['W2'] = np.load(w2_path)
    if w3_path:
        out['W3'] = np.load(w3_path)
    return out


def predict_race_probs(X, mask, scores, line_ids, line_pos, W1, W2, W3,
                       top_k_a=None, prob_floor=1e-10):
    """1レースの P(a,b,c) を車番キーで返す。"""
    P1 = forward(X[None], W1, mask[None])[0]  # (9,)
    a_order = [int(a) for a in np.argsort(-P1) if mask[a] > 0]
    if top_k_a is not None:
        a_order = a_order[:top_k_a]
    probs = {}
    for a in a_order:
        if P1[a] < prob_floor:
            continue
        X2, mask2 = build_X2(X, scores, line_ids, line_pos, mask, a)
        P2 = forward(X2[None], W2, mask2[None])[0]
        for b in np.where(mask2 > 0)[0]:
            b = int(b)
            if P2[b] < prob_floor:
                continue
            X3, mask3 = build_X3(X, scores, line_ids, line_pos, mask, a, b)
            P3 = forward(X3[None], W3, mask3[None])[0]
            for c in np.where(mask3 > 0)[0]:
                c = int(c)
                if P3[c] < prob_floor:
                    continue
                probs[(a, b, c)] = float(P1[a] * P2[b] * P3[c])
    return probs


def to_car_probs(probs, cars):
    """スロット index → 車番 へ変換。"""
    return {(int(cars[a]), int(cars[b]), int(cars[c])): p
            for (a, b, c), p in probs.items()}


def predict_race_cars(race_idx, data, top_k_a=None):
    probs = predict_race_probs(
        data['X'][race_idx], data['mask'][race_idx],
        data['scores'][race_idx], data['line_ids'][race_idx],
        data['line_pos'][race_idx], data['W1'], data['W2'], data['W3'],
        top_k_a=top_k_a,
    )
    return to_car_probs(probs, data['cars'][race_idx])


def trifecta_ev(probs_cars, odds_table, stake=100):
    """odds_table[(a,b,c)] = 払戻倍率。probs_cars は車番キー。"""
    rows = []
    for k, p in probs_cars.items():
        if k not in odds_table:
            continue
        ev = p * odds_table[k] * stake - stake
        rows.append((k, p, odds_table[k], ev))
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows

def _fwd(W, X, mask):
    """W が dict なら MLP、ndarray なら線形。"""
    if isinstance(W, dict):
        P, _ = forward_mlp(X, W, mask)
        return P
    return forward(X, W, mask)


def predict_race_probs_mixed(X, mask, scores, line_ids, line_pos, W1, W2, W3,
                              top_k_a=None, prob_floor=1e-10):
    """W1/W2/W3 それぞれが dict (MLP) か ndarray (linear) か自動判定して計算。"""
    P1 = _fwd(W1, X[None], mask[None])[0]
    a_order = [int(a) for a in np.argsort(-P1) if mask[a] > 0]
    if top_k_a is not None:
        a_order = a_order[:top_k_a]
    probs = {}
    for a in a_order:
        if P1[a] < prob_floor:
            continue
        X2, mask2 = build_X2(X, scores, line_ids, line_pos, mask, a)
        P2 = _fwd(W2, X2[None], mask2[None])[0]
        for b in np.where(mask2 > 0)[0]:
            b = int(b)
            if P2[b] < prob_floor:
                continue
            X3, mask3 = build_X3(X, scores, line_ids, line_pos, mask, a, b)
            P3 = _fwd(W3, X3[None], mask3[None])[0]
            for c in np.where(mask3 > 0)[0]:
                c = int(c)
                if P3[c] < prob_floor:
                    continue
                probs[(a, b, c)] = float(P1[a] * P2[b] * P3[c])
    return probs
