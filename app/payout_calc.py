"""公式払戻金の解析と、誤って書き換えた払戻の復元判定(DB非依存)。

3連単の払戻の位置: 結果ページの払戻一覧は 7件 または 9件 のとき、最後の1件が3連単。
(14,324レースで「最後の1件 = 保存オッズ×100円」が 7件=99.6% / 9件=99.4% で一致することを確認済み。
 以前の『7番目=3連単』という決め打ちは、9件のページで誤っていた。)
"""
import re


def parse_yen(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d,]+", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_trifecta_payout(payouts):
    """payouts: [{'賭式':..., '払戻金': '12,340円'}, ...]。7件または9件のときだけ最後の1件を3連単として返す。
    それ以外(同着などで件数が違う)は None(保存しない)。"""
    if not payouts or len(payouts) not in (7, 9):
        return None
    last = payouts[-1]
    y = parse_yen(last.get("払戻金")) if isinstance(last, dict) else None
    return y if (y is not None and y >= 100) else None


def restore_value_purchase(stake, current, final_odds, odds_at_purchase, payout_3t):
    """誤った公式払戻金で書き換えられた購入の払戻だけを、元の計算(確定オッズ×購入額)へ戻す。
    書き換え済み(現在値 == 公式由来の値)かつ 元の値と異なる場合のみ新しい値を返す。それ以外は None。"""
    if not stake or not payout_3t:
        return None
    ov = final_odds if final_odds else odds_at_purchase
    if not ov:
        return None
    original = round(stake * ov)
    official = round(stake * payout_3t / 100.0)
    if current is None or round(current) != official or official == original:
        return None
    return float(original)


def restore_value_skipped(current, odds_value, payout_3t):
    if not payout_3t or not odds_value or current is None:
        return None
    original = round(100.0 * odds_value)
    official = round(payout_3t)
    if round(current) != official or official == original:
        return None
    return float(original)
