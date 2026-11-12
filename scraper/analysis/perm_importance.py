import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, train, forward, logloss_1st

FEATURE_NAMES = [
    "競走得点", "勝率", "2連対率", "3連対率",
    "逃", "捲", "差", "マ", "S", "H", "B",
    "ギア倍数", "年齢", "期",
    "ライン位置", "ライン規模", "得点偏差",
    "脚質", "級班", "枠番",
    "地元", "コメント_自力自在", "コメント_自力",
    "コメント_番手", "コメント_人名", "コメント_長さ", "コメント_空"
]

def perm_importance(X, y, mask, W, n_repeats=5, seed=42):
    """各特徴をシャッフルして対数損失の増加を測る。"""
    rng = np.random.default_rng(seed)
    P = forward(X, W, mask)
    base_ll = logloss_1st(P, y)
    print(f"ベースライン対数損失: {base_ll:.4f}")
    importances = []
    D = X.shape[2]
    for d in range(D):
        deltas = []
        for _ in range(n_repeats):
            X_perm = X.copy()
            # 特徴dをシャッフル（行方向）
            X_perm[:, :, d] = rng.permutation(X_perm[:, :, d].flatten()).reshape(X_perm.shape[0], X_perm.shape[1])
            P_perm = forward(X_perm, W, mask)
            ll = logloss_1st(P_perm, y)
            deltas.append(ll - base_ll)
        importances.append({'idx': d, 'name': FEATURE_NAMES[d], 'delta': np.mean(deltas), 'std': np.std(deltas)})
    return importances

if __name__ == '__main__':
    tr, te = load_data()
    W = np.load(f'{_HERE}/data/W_1st.npy')
    print(f"検証データ: {te['X'].shape}")
    imps = perm_importance(te['X'], te['y'][:, 0], te['mask'], W, n_repeats=5)
    imps.sort(key=lambda x: -x['delta'])
    print('\n=== 特徴量重要度（対数損失の増加）===')
    print(f"{'順位':>4} {'特徴':<18} {'重要度':>10} {'標準偏差':>10}")
    for rank, imp in enumerate(imps, 1):
        print(f"{rank:>4} {imp['name']:<18} {imp['delta']:>10.4f} {imp['std']:>10.4f}")
