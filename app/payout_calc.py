"""公式払戻金の解析(DB非依存)。"""
import re


def parse_yen(v):
    """'12,340円' / 12340 → 12340.0。不正は None。"""
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
    """result.payouts から3連単の払戻金(100円あたり・円)を取り出す。
    3連単が丁度1件で100円以上のときのみ返す(曖昧・不正は None)。"""
    if not payouts:
        return None
    hits = [p for p in payouts if isinstance(p, dict) and p.get("賭式") == "3連単"]
    if len(hits) != 1:
        return None
    y = parse_yen(hits[0].get("払戻金"))
    return y if (y is not None and y >= 100) else None
