import numpy as np
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, train, forward, logloss_1st, brier_1st, accuracy_top1, accuracy_top3

if __name__ == '__main__':
    tr, te = load_data()
    print(f"訓練: X3={tr['X3'].shape}, y3={tr['y3'].shape}")
    print(f"検証: X3={te['X3'].shape}, y3={te['y3'].shape}")
    W3 = train(tr['X3'], tr['y3'], tr['mask3'], te['X3'], te['y3'], te['mask3'])
    np.save(f'{_HERE}/data/W_3rd.npy', W3)
    P3_te = forward(te['X3'], W3, te['mask3'])
    print(f"\n=== 最終（3着モデル、35特徴）===")
    print(f"3着対数損失: {logloss_1st(P3_te, te['y3']):.4f}")
    print(f"3着ブライアスコア: {brier_1st(P3_te, te['y3'], te['mask3']):.5f}")
    print(f"3着Top1精度: {accuracy_top1(P3_te, te['y3']):.4f}")
    print(f"3着Top3精度: {accuracy_top3(P3_te, te['y3'], te['mask3']):.4f}")
