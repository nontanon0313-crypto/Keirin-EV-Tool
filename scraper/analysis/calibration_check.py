import numpy as np, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, forward

def calibration_table(P, y, mask, n_bins=10):
    """予測確率のビン別に、実績的中率を集計。"""
    N = P.shape[0]
    Y = np.zeros_like(P)
    Y[np.arange(N), y] = 1.0
    bins = [[] for _ in range(n_bins)]
    for i in range(N):
        for k in range(P.shape[1]):
            if mask[i, k] == 0: continue
            p = P[i, k]
            label = Y[i, k]
            b = min(int(p * n_bins), n_bins - 1)
            bins[b].append((p, label))
    print(f"{'Bin':>8} {'n':>6} {'予測平均':>10} {'実績':>10} {'差':>10}")
    for b, items in enumerate(bins):
        if not items: continue
        n = len(items)
        pred_avg = np.mean([x[0] for x in items])
        actual = np.mean([x[1] for x in items])
        diff = actual - pred_avg
        print(f"{b/n_bins:.1f}-{(b+1)/n_bins:.1f}  {n:>6} {pred_avg:>10.4f} {actual:>10.4f} {diff:>+10.4f}")

if __name__ == '__main__':
    tr, te = load_data()
    W = np.load(f'{_HERE}/data/W_1st.npy')
    P = forward(te['X'], W, te['mask'])
    print(f"検証データ: {te['X'].shape[0]}レース\n")
    calibration_table(P, te['y'][:, 0], te['mask'], n_bins=10)
