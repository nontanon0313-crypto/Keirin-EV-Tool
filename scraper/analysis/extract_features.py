import json, glob, os, sys
import numpy as np

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

def rider_features(r, line_map, pos_map, race_scores):
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
    ]
