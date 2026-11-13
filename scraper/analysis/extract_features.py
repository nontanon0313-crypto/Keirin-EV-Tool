import json, glob, os, sys
import numpy as np
from venue_region import venue_prefecture

GRADE_CLASSES = {
    "A級1班": 0, "A級2班": 1, "A級3班": 2,
    "S級1班": 3, "S級2班": 4,
    "L級1班": 5,
    "Ａ１": 0, "Ａ２": 1, "Ａ３": 2, "Ｓ１": 3, "Ｓ２": 4,
}
LEG_STYLES = {"逃": 0, "追": 1, "両": 2}
WAKU_CLASSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}

# 追加: レースグレード
RACE_GRADE = {
    "F1": 0, "F2": 1,
    "G1": 2, "G2": 3, "G3": 4,
    "GP": 5,
}
# 追加: レース種別（race_name から推定）
def _race_type(race_name):
    rn = str(race_name or "")
    if "決勝" in rn: return 2
    if "準決" in rn or "準々" in rn: return 1
    if "予選" in rn or "一般" in rn or "特選" in rn: return 0
    return -1

def safe_float(v, default=0.0):
    try: return float(v)
    except (ValueError, TypeError): return default

def safe_int(v, default=0):
    try: return int(v)
    except (ValueError, TypeError): return default

def extract_one_race(path):
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        return None
    riders = d['entry'].get('riders', [])
    if len(riders) < 5:
        return None
    lines = d['entry'].get('lines') or []
    if not lines:
        return None
    line_map, pos_map = {}, {}
    for li, line in enumerate(lines):
        for pos, car in enumerate(line):
            line_map[int(car)] = li
            pos_map[int(car)] = pos
    return riders, line_map, pos_map, d

def _recent_stats(r):
    """直近着順 {1着,2着,3着,着外} から率を計算。"""
    rec = r.get("直近着順")
    if not isinstance(rec, dict):
        return 0.0, 0.0, 0.0
    n1 = safe_int(rec.get("1着"))
    n2 = safe_int(rec.get("2着"))
    n3 = safe_int(rec.get("3着"))
    no = safe_int(rec.get("着外"))
    tot = n1 + n2 + n3 + no
    if tot <= 0: return 0.0, 0.0, 0.0
    return n1/tot, (n1+n2)/tot, (n1+n2+n3)/tot

def rider_features(r, line_map, pos_map, race_scores, venue_name=None,
                   race_ctx=None):
    """1選手の特徴ベクトル(34次元)。race_ctx はレース単位の付加特徴（全選手同値）。"""
    car = safe_int(r.get("車番"))
    score = safe_float(r.get("競走得点"))
    race_mean = np.mean(race_scores)
    race_std = np.std(race_scores) or 1.0
    line_id = line_map.get(car, -1)
    line_pos = pos_map.get(car, 99)
    line_size = sum(1 for v in line_map.values() if v == line_id) if line_id >= 0 else 1
    r1, r12, r123 = _recent_stats(r)
    ctx = race_ctx or {}
    return [
        score,
        safe_float(r.get("勝率")) / 100,
        safe_float(r.get("2連対率")) / 100,
        safe_float(r.get("3連対率")) / 100,
        safe_int(r.get("逃")), safe_int(r.get("捲")),
        safe_int(r.get("差")), safe_int(r.get("マ")),
        safe_int(r.get("S")), safe_int(r.get("H")), safe_int(r.get("B")),
        safe_float(r.get("ギア倍数")),
        safe_int(r.get("年齢")), safe_int(r.get("期")),
        line_pos if line_pos < 3 else 3,
        line_size,
        (score - race_mean) / race_std,
        LEG_STYLES.get(r.get("脚質"), -1),
        GRADE_CLASSES.get(r.get("級班"), -1),
        WAKU_CLASSES.get(safe_int(r.get("枠番")), -1),
        _is_local(r, venue_name),
        *_comment_features(r.get("コメント") or ""),
        # 追加3次元: 今場所前場所（平均着順・連対率・特異マーカー率）
        *_line_dynamics_features(r, line_map, pos_map, ctx.get("score_by_car", {})),
    ]

def _split_kyo_prev(s):
    """今場所前場所 を 今場所部 / 前場所部 に分割。"""
    VENUE_CHARS = set('ＦＧＰＬｆｇｐｌFGPLfgpl')
    if not s: return '', ''
    s = s.strip()
    tokens = s.split()
    venue_idxs = []
    for i, tok in enumerate(tokens):
        if len(tok) >= 3 and any(c in VENUE_CHARS for c in tok):
            venue_idxs.append(i)
    if len(venue_idxs) >= 2:
        i2 = venue_idxs[1]
        return ' '.join(tokens[:i2]), ' '.join(tokens[i2:])
    return s, ''


def _parse_kyo_basho(s):
    """前場所部分のみをパースして3次元を返す（今場所は未来情報のため除外）。
    Slot1: 平均着順（数値着順 1-9 のみの平均。0件なら 0）
    Slot2: 連対率（1-2着の割合。0件なら 0）
    Slot3: 特異マーカー率（S/H/B/落/途欠/病/補 等の割合）
    """
    if not s or not isinstance(s, str):
        return 0.0, 0.0, 0.0
    _, prev = _split_kyo_prev(s)
    if not prev:
        return 0.0, 0.0, 0.0
    trans = str.maketrans(
        '０１２３４５６７８９ＦＧＰＬｆｇｐｌＳＨＢ',
        '0123456789FGPLfgplSHB'
    )
    s2 = prev.translate(trans)
    tokens = s2.split()
    VENUE_CHARS = set('FGPLfgpl')
    numerics = []
    markers = 0
    for tok in tokens:
        if any(c in VENUE_CHARS for c in tok):
            continue
        if tok.isdigit():
            v = int(tok)
            if 1 <= v <= 9:
                numerics.append(v)
                continue
        markers += 1
    total = len(numerics) + markers
    if total == 0:
        return 0.0, 0.0, 0.0
    avg_fin = float(sum(numerics)) / len(numerics) if numerics else 0.0
    rentai = sum(1 for v in numerics if v <= 2) / len(numerics) if numerics else 0.0
    abnormal = markers / total
    return avg_fin, rentai, abnormal


def _line_dynamics_features(r, line_map, pos_map, score_by_car):
    """ライン力学の3特徴（A, B, C）:
    A: 自ライン先頭の競走得点 - 自分の競走得点
    B: 同ラインで自分より競走得点が高い選手数
    C: 自分の車番±1に他ラインの先頭車がいるか (0/1)
    """
    car = safe_int(r.get("車番"))
    score = safe_float(r.get("競走得点"))
    line_id = line_map.get(car, -1)
    if line_id < 0 or not score_by_car:
        return 0.0, 0.0, 0.0
    leader_car = None
    for c, lid in line_map.items():
        if lid == line_id and pos_map.get(c) == 0:
            leader_car = c
            break
    a_val = 0.0
    if leader_car is not None:
        a_val = score_by_car.get(leader_car, 0.0) - score
    b_val = 0
    for c, lid in line_map.items():
        if lid == line_id and c != car:
            if score_by_car.get(c, -999) > score:
                b_val += 1
    c_val = 0.0
    my_line = line_map.get(car, -1)
    for nb in (car - 1, car + 1):
        if nb in line_map and pos_map.get(nb) == 0 and line_map[nb] != my_line:
            c_val = 1.0
            break
    return float(a_val), float(b_val), c_val


def _race_ctx(d):
    """レース単位の付加特徴。全選手で同値。"""
    entry = d.get('entry', {})
    grade = entry.get('grade', '')
    rn = entry.get('race_name', '')
    post_iso = entry.get('post_time_iso', '')
    hour = -1
    if post_iso and 'T' in post_iso:
        try:
            hour = int(post_iso.split('T')[1][:2])
        except (ValueError, IndexError):
            pass
    race_no = safe_int(d.get('race_no', 0))
    return {
        "race_grade": RACE_GRADE.get(str(grade), -1),
        "race_type": _race_type(rn),
        "post_hour": hour,
        "is_last_race": 1 if race_no >= 11 else 0,
    }

def _is_local(r, venue_name):
    if not venue_name:
        return -1
    pref = venue_prefecture(venue_name)
    if not pref:
        return -1
    player_pref = (r.get("地区") or "").strip()
    if not player_pref:
        return -1
    return 1 if player_pref == pref else 0

def _comment_features(c):
    c = c.strip()
    return [
        1 if "自力自在" in c else 0,
        1 if ("自力" in c and "自力自在" not in c) else 0,
        1 if "番手" in c else 0,
        1 if (("君" in c or "さん" in c) and "自力" not in c and "番手" not in c) else 0,
        len(c),
        1 if not c else 0,
    ]
