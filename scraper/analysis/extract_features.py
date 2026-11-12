import json, glob, os, sys
import numpy as np
from venue_region import venue_prefecture

# 級班: A級/S級/L級(ガールズ) × 1班/2班/3班。全角表記ゆれも吸収
GRADE_CLASSES = {
    "A級1班": 0, "A級2班": 1, "A級3班": 2,
    "S級1班": 3, "S級2班": 4,
    "L級1班": 5,
    "Ａ１": 0, "Ａ２": 1, "Ａ３": 2, "Ｓ１": 3, "Ｓ２": 4,
}
# 脚質: 逃/追/両の3種類のみ
LEG_STYLES = {"逃": 0, "追": 1, "両": 2}
WAKU_CLASSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}

def safe_float(v, default=0.0):
    try: return float(v)
    except (ValueError, TypeError): return default

def safe_int(v, default=0):
    try: return int(v)
    except (ValueError, TypeError): return default

def extract_one_race(path):
    """1レース分を読み、出走選手とライン情報を返す。"""
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception:
        return None
    riders = d['entry'].get('riders', [])
    if len(riders) < 7:
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

def rider_features(r, line_map, pos_map, race_scores, venue_name=None):
    """1選手の特徴ベクトル(20次元)。"""
    car = safe_int(r.get("車番"))
    score = safe_float(r.get("競走得点"))
    race_mean = np.mean(race_scores)
    race_std = np.std(race_scores) or 1.0
    line_id = line_map.get(car, -1)
    line_pos = pos_map.get(car, 99)
    line_size = sum(1 for v in line_map.values() if v == line_id) if line_id >= 0 else 1
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
        # 地元フラグ (地区一致=1, 不一致=0, 不明=-1)
        _is_local(r, venue_name),
        # コメント特徴 (6次元を展開)
        *_comment_features(r.get("コメント") or ""),
    ]

def _is_local(r, venue_name):
    """選手の出身地(地区=都道府県)とバンク所在地(都道府県)が一致すれば1, 不一致0, 不明-1。"""
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
    """コメントを特徴量化。(6次元)"""
    c = c.strip()
    return [
        1 if "自力自在" in c else 0,
        1 if ("自力" in c and "自力自在" not in c) else 0,
        1 if "番手" in c else 0,
        1 if (("君" in c or "さん" in c) and "自力" not in c and "番手" not in c) else 0,
        len(c),
        1 if not c else 0,
    ]
