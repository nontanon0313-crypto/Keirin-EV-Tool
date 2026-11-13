import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

def load_data():
    tr = np.load(f'{_HERE}/data/train.npz')
    te = np.load(f'{_HERE}/data/test.npz')
    norm = np.load(f'{_HERE}/data/norm.npz')
    mean, std = norm['mean'], norm['std']
    tr_X = (tr['X'] - mean) / std
    te_X = (te['X'] - mean) / std
    tr_X = tr_X * tr['mask'][..., None]
    te_X = te_X * te['mask'][..., None]
    # X2: 27次元部分のみ標準化。28〜31列は関係特徴でスケール済み（pos_diff等）→ 標準化対象外
    tr_X2 = tr['X2'].copy(); te_X2 = te['X2'].copy()
    tr_X2[:, :, :27] = ((tr['X2'][:, :, :27] - mean) / std) * tr['mask2'][..., None]
    te_X2[:, :, :27] = ((te['X2'][:, :, :27] - mean) / std) * te['mask2'][..., None]
    tr_X2 = tr_X2 * tr['mask2'][..., None]
    te_X2 = te_X2 * te['mask2'][..., None]
    return ({'X': tr_X, 'mask': tr['mask'], 'n': tr['n'], 'y': tr['y'],
             'X2': tr_X2, 'mask2': tr['mask2'], 'y2': tr['y2']},
            {'X': te_X, 'mask': te['mask'], 'n': te['n'], 'y': te['y'],
             'X2': te_X2, 'mask2': te['mask2'], 'y2': te['y2']})

def softmax_masked(scores, mask):
    """scores:(N,L), mask:(N,L)。パディング位置は確率0。"""
    scores = np.where(mask > 0, scores, -1e9)
    m = scores.max(axis=-1, keepdims=True)
    e = np.exp(scores - m) * mask
    return e / (e.sum(axis=-1, keepdims=True) + 1e-12)

def forward(X, W, mask):
    """X:(N,L,D), W:(D,), mask:(N,L) → P:(N,L)"""
    return softmax_masked(X @ W, mask)

def loss_and_grad(X, y, mask, W, l2=1e-4):
    """y:(N,) クラスラベル。"""
    N = X.shape[0]
    P = forward(X, W, mask)
    ce = -np.log(P[np.arange(N), y] + 1e-12).mean()
    loss = ce + 0.5 * l2 * (W ** 2).sum()
    Y = np.zeros_like(P)
    Y[np.arange(N), y] = 1.0
    dS = (P - Y) / N  # (N, L)
    dW = np.einsum('nld,nl->d', X, dS) + l2 * W  # (D,)
    return loss, dW

def brier_1st(P, y, mask):
    """有効車のみでブライアスコア。"""
    N = P.shape[0]
    Y = np.zeros_like(P)
    Y[np.arange(N), y] = 1.0
    sq = ((P - Y) ** 2).sum(axis=1)
    return sq.mean() / mask.sum(axis=1).mean()

def logloss_1st(P, y):
    return -np.log(P[np.arange(len(y)), y] + 1e-12).mean()

def accuracy_top1(P, y):
    return (P.argmax(axis=1) == y).mean()

def accuracy_top3(P, y, mask):
    """実績1着車が予測top3に入っている率。"""
    N = P.shape[0]
    hit = 0
    for i in range(N):
        k = int(mask[i].sum())
        top3 = np.argsort(-P[i])[:3]
        if y[i] in top3: hit += 1
    return hit / N

def train(X_tr, y_tr, m_tr, X_te, y_te, m_te, lr=0.01, epochs=50, batch=256, l2=1e-4, seed=42):
    rng = np.random.default_rng(seed)
    D = X_tr.shape[2]
    W = rng.normal(0, 0.01, size=D).astype(np.float32)
    N = X_tr.shape[0]
    for ep in range(epochs):
        perm = rng.permutation(N)
        for i in range(0, N, batch):
            idx = perm[i:i+batch]
            _, dW = loss_and_grad(X_tr[idx], y_tr[idx], m_tr[idx], W, l2)
            norm = np.sqrt((dW ** 2).sum())
            if norm > 1.0:
                dW = dW / norm
            W -= lr * dW
        P_tr = forward(X_tr, W, m_tr)
        P_te = forward(X_te, W, m_te)
        tr_ll = logloss_1st(P_tr, y_tr)
        te_ll = logloss_1st(P_te, y_te)
        te_bs = brier_1st(P_te, y_te, m_te)
        te_acc = accuracy_top1(P_te, y_te)
        print(f"Ep{ep+1:2d} tr_ll={tr_ll:.4f} te_ll={te_ll:.4f} te_bs={te_bs:.5f} te_acc={te_acc:.4f}")
    return W

if __name__ == '__main__':
    tr, te = load_data()
    print(f"訓練: {tr['X'].shape}, 検証: {te['X'].shape}")
    W = train(tr['X'], tr['y'][:,0], tr['mask'], te['X'], te['y'][:,0], te['mask'])
    np.save(f'{_HERE}/data/W_1st.npy', W)
    P_te = forward(te['X'], W, te['mask'])
    print(f"\n=== 最終（1着モデル、全27特徴）===")
    print(f"1着対数損失: {logloss_1st(P_te, te['y'][:,0]):.4f}")
    print(f"1着ブライアスコア: {brier_1st(P_te, te['y'][:,0], te['mask']):.5f}")
    print(f"1着Top1精度: {accuracy_top1(P_te, te['y'][:,0]):.4f}")
    print(f"1着Top3精度: {accuracy_top3(P_te, te['y'][:,0], te['mask']):.4f}")
