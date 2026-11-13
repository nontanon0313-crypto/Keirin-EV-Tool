import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, train, forward, logloss_1st, brier_1st, accuracy_top1, accuracy_top3

if __name__ == '__main__':
    tr, te = load_data()
    print(f"訓練: X2={tr['X2'].shape}, y2={tr['y2'].shape}")
    print(f"検証: X2={te['X2'].shape}, y2={te['y2'].shape}")
    W2 = train(tr['X2'], tr['y2'], tr['mask2'], te['X2'], te['y2'], te['mask2'])
    np.save(f'{_HERE}/data/W_2nd.npy', W2)
    P2_te = forward(te['X2'], W2, te['mask2'])
    print(f"\n=== 最終（2着モデル、31特徴）===")
    print(f"2着対数損失: {logloss_1st(P2_te, te['y2']):.4f}")
    print(f"2着ブライアスコア: {brier_1st(P2_te, te['y2'], te['mask2']):.5f}")
    print(f"2着Top1精度: {accuracy_top1(P2_te, te['y2']):.4f}")
    print(f"2着Top3精度: {accuracy_top3(P2_te, te['y2'], te['mask2']):.4f}")
