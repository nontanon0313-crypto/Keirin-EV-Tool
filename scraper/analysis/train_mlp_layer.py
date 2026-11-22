import numpy as np, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from train_softmax import load_data, logloss_1st, accuracy_top1
from train_mlp import train_mlp, forward


def main(layer, h1=32, h2=None):
    tr, te = load_data()
    if layer == '1st':
        X_tr, y_tr, m_tr = tr['X'], tr['y'][:,0], tr['mask']
        X_te, y_te, m_te = te['X'], te['y'][:,0], te['mask']
        out = 'W_1st_mlp.npz'
    elif layer == '2nd':
        X_tr, y_tr, m_tr = tr['X2'], tr['y2'], tr['mask2']
        X_te, y_te, m_te = te['X2'], te['y2'], te['mask2']
        out = 'W_2nd_mlp.npz'
    elif layer == '3rd':
        X_tr, y_tr, m_tr = tr['X3'], tr['y3'], tr['mask3']
        X_te, y_te, m_te = te['X3'], te['y3'], te['mask3']
        out = 'W_3rd_mlp.npz'
    else:
        raise ValueError(layer)

    print('Layer=%s  X_tr=%s  X_te=%s' % (layer, X_tr.shape, X_te.shape))
    params = train_mlp(X_tr, y_tr, m_tr, X_te, y_te, m_te, H=h1, H2=h2)
    if h2 is not None:
        out = out.replace('.npz', f'_h{h1}_{h2}.npz')
    else:
        out = out.replace('.npz', f'_h{h1}.npz')
    np.savez(f'{_HERE}/data/{out}', **params)
    print(f'saved: {out}')
    P_te, _ = forward(X_te, params, m_te)
    print(f'=== MLP {layer} 最終 ===')
    print(f'log loss: {logloss_1st(P_te, y_te):.4f}')
    print(f'Top1: {accuracy_top1(P_te, y_te):.4f}')


if __name__ == '__main__':
    layer = sys.argv[1]
    h1 = int(sys.argv[2]) if len(sys.argv) > 2 else 32
    h2 = int(sys.argv[3]) if len(sys.argv) > 3 else None
    main(layer, h1=h1, h2=h2)
