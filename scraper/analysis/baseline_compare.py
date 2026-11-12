import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, train, forward, logloss_1st, brier_1st, accuracy_top1, accuracy_top3

def pick_feature(X, idx):
    """指定indexの特徴のみ残し、他を0に。"""
    X_out = np.zeros_like(X)
    X_out[..., idx] = X[..., idx]
    return X_out

def eval_model(name, X_tr, y_tr, m_tr, X_te, y_te, m_te):
    W = train(X_tr, y_tr, m_tr, X_te, y_te, m_te)
    P = forward(X_te, W, m_te)
    ll = logloss_1st(P, y_te)
    bs = brier_1st(P, y_te, m_te)
    t1 = accuracy_top1(P, y_te)
    t3 = accuracy_top3(P, y_te, m_te)
    print(f"\n=== {name} ===")
    print(f"  対数損失: {ll:.4f}  ブライアスコア: {bs:.5f}  Top1: {t1:.4f}  Top3: {t3:.4f}")
    return {'name': name, 'll': ll, 'bs': bs, 'top1': t1, 'top3': t3, 'W': W}

if __name__ == '__main__':
    tr, te = load_data()
    print(f"訓練: {tr['X'].shape}, 検証: {te['X'].shape}")

    results = []

    # 1. 競走得点のみ (index 0)
    X_tr0 = pick_feature(tr['X'], 0)
    X_te0 = pick_feature(te['X'], 0)
    results.append(eval_model("競走得点のみ", X_tr0, tr['y'][:,0], tr['mask'],
                              X_te0, te['y'][:,0], te['mask']))

    # 2. 競走得点 + 勝率 (index 1)
    X_tr1 = tr['X'].copy(); X_tr1[..., 2:] = 0
    X_te1 = te['X'].copy(); X_te1[..., 2:] = 0
    results.append(eval_model("競走得点+勝率", X_tr1, tr['y'][:,0], tr['mask'],
                              X_te1, te['y'][:,0], te['mask']))

    # 3. 競走得点+勝率+2連+3連 (index 0〜3)
    X_tr2 = tr['X'].copy(); X_tr2[..., 4:] = 0
    X_te2 = te['X'].copy(); X_te2[..., 4:] = 0
    results.append(eval_model("能力指標4つ", X_tr2, tr['y'][:,0], tr['mask'],
                              X_te2, te['y'][:,0], te['mask']))

    # 4. 全27特徴
    results.append(eval_model("全27特徴", tr['X'], tr['y'][:,0], tr['mask'],
                              te['X'], te['y'][:,0], te['mask']))

    print("\n=== 比較表 ===")
    print(f"{'モデル':<15} {'対数損失':>10} {'Top1':>8} {'Top3':>8}")
    for r in results:
        print(f"{r['name']:<15} {r['ll']:>10.4f} {r['top1']:>8.4f} {r['top3']:>8.4f}")
    np.save(f'{_HERE}/data/W_score_only.npy', results[0]['W'])
