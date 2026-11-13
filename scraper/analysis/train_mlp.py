import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, softmax_masked, logloss_1st, brier_1st, accuracy_top1, accuracy_top3


def init_params(D, H, rng, scale=0.1):
    return {
        'W1': (rng.normal(0, scale, size=(D, H))).astype(np.float32),
        'b1': np.zeros(H, dtype=np.float32),
        'W2': (rng.normal(0, scale, size=(H,))).astype(np.float32),
        'b2': np.float32(0.0),
    }


def forward(X, params, mask):
    h = np.maximum(X @ params['W1'] + params['b1'], 0)
    scores = h @ params['W2'] + params['b2']
    return softmax_masked(scores, mask), h


def loss_and_grad(X, y, mask, params, l2=1e-4):
    N = X.shape[0]
    P, h = forward(X, params, mask)
    ce = -np.log(P[np.arange(N), y] + 1e-12).mean()
    loss = ce + 0.5 * l2 * ((params['W1'] ** 2).sum() + (params['W2'] ** 2).sum())
    Y = np.zeros_like(P); Y[np.arange(N), y] = 1.0
    dS = (P - Y) / N
    dW2 = np.einsum('nlh,nl->h', h, dS) + l2 * params['W2']
    db2 = np.float32(dS.sum())
    dh = dS[:, :, None] * params['W2'][None, None, :] * (h > 0).astype(h.dtype)
    dW1 = np.einsum('nld,nlh->dh', X, dh) + l2 * params['W1']
    db1 = dh.sum(axis=(0, 1))
    return loss, {'W1': dW1, 'b1': db1, 'W2': dW2, 'b2': db2}


def train_mlp(X_tr, y_tr, m_tr, X_te, y_te, m_te, H=32, lr=1e-3, epochs=30, batch=256, l2=1e-4, seed=42, patience=5):
    rng = np.random.default_rng(seed)
    D = X_tr.shape[2]
    params = init_params(D, H, rng)
    m_opt = {k: np.zeros_like(v) for k, v in params.items()}
    v_opt = {k: np.zeros_like(v) for k, v in params.items()}
    t = 0; beta1, beta2, eps = 0.9, 0.999, 1e-8
    N = X_tr.shape[0]
    best_te_ll = float('inf'); best_params = None; patience_left = patience
    for ep in range(epochs):
        perm = rng.permutation(N)
        for i in range(0, N, batch):
            idx = perm[i:i+batch]
            _, grads = loss_and_grad(X_tr[idx], y_tr[idx], m_tr[idx], params, l2)
            t += 1
            for k in params:
                m_opt[k] = beta1 * m_opt[k] + (1 - beta1) * grads[k]
                v_opt[k] = beta2 * v_opt[k] + (1 - beta2) * (grads[k] ** 2)
                m_hat = m_opt[k] / (1 - beta1 ** t)
                v_hat = v_opt[k] / (1 - beta2 ** t)
                params[k] = params[k] - lr * m_hat / (np.sqrt(v_hat) + eps)
        P_tr, _ = forward(X_tr, params, m_tr)
        P_te, _ = forward(X_te, params, m_te)
        tr_ll = logloss_1st(P_tr, y_tr)
        te_ll = logloss_1st(P_te, y_te)
        te_acc = accuracy_top1(P_te, y_te)
        print(f"Ep{ep+1:2d} tr_ll={tr_ll:.4f} te_ll={te_ll:.4f} te_acc={te_acc:.4f}")
        if te_ll < best_te_ll - 1e-4:
            best_te_ll = te_ll
            best_params = {k: v.copy() for k, v in params.items()}
            patience_left = patience
        else:
            patience_left -= 1
            if patience_left <= 0:
                print(f"Early stop at Ep{ep+1}")
                break
    return best_params


if __name__ == '__main__':
    tr, te = load_data()
    print(f"訓練: {tr['X'].shape}, 検証: {te['X'].shape}")
    params = train_mlp(tr['X'], tr['y'][:,0], tr['mask'], te['X'], te['y'][:,0], te['mask'])
    np.savez(f'{_HERE}/data/W_1st_mlp.npz', **params)
    P_te, _ = forward(te['X'], params, te['mask'])
    print(f"\n=== MLP 最終 ===")
    print(f"log loss: {logloss_1st(P_te, te['y'][:,0]):.4f}")
    print(f"Top1: {accuracy_top1(P_te, te['y'][:,0]):.4f}")
