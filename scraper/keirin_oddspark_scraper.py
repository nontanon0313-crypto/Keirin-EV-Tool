#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odds Park 競輪 スクレイパー（耐障害版 v2）

v2での変更点(のんの報告した「オッズ欠損」「読み込みタイムアウト」を受けて全面修正):

1. 致命的なバグ: オッズ値と人気順位の2セルを両方「1車番分のデータ」として
   数えてしまい、車番の対応が1つずつズレていた。ペア単位で消費するよう修正。

2. 構造の誤り: 実際のoddspark.comのページ(2026年8月に実URLで確認)を調べたところ、
   券種ごとに全く異なる表構造だった。以前は全券種を同じ「1着/2着/3着」3項目の
   ロジックで無理やり解釈しており、車番の対応が誤っていた。
   - 2車複・ワイド: 車番の若い方を列、大きい方を行とした三角形の1ページ完結マトリクス
     (例:7車立てなら21通りぴったり。軸ごとの取得は不要)
   - 2車単: 列=1着車番、行=2着車番の全体マトリクス(1ページ完結、n×(n-1)通り)
   - 3連単: 軸車番(1着候補)ごとに個別ページ(1〜9を並列取得して合算)
   - 3連複: 3連単と同様、軸車番ごとに個別ページが必要と判明
     (以前は1ページだけ取得しており、軸1相当の一部データしか取れていなかった)

3. タイムアウトを(5,10)→(5,20)に延長し、リトライ回数・バックオフを強化
   (実データ24レースの大半の欠損は"Read timed out"が原因だった)

4. 軸ごと取得で失敗した軸だけをシリアルで最大2回まで追い取得する
   (以前は1回失敗したらその軸を諦めていた)

5. 選手数から券種ごとの理論組み合わせ数を計算し、実際の取得数と比較して
   is_complete(欠損なしか)を各オッズデータに付与

6. 出走表・結果の取得もタイムアウトした場合は1回だけ自動で追いリトライする
"""
import requests
from bs4 import BeautifulSoup
import json, time, re, os, random, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
BASE = "https://www.oddspark.com/keirin"
OUTPUT_DIR = os.environ.get("KEIRIN_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

BET_TYPES = {5: "2車複", 6: "2車単", 7: "ワイド", 8: "3連複", 9: "3連単"}
# 軸車番ごとにページを分けて取得する必要がある券種(2026年8月に実ページで確認)
AXIS_BASED_BET_TYPES = {8, 9}

_session = None

def get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session

def get_soup(url, params=None, retries=4, timeout=(5, 20)):
    last_err = None
    for i in range(retries):
        try:
            r = get_session().get(url, params=params, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(min(1.0 * (2 ** i) + random.uniform(0, 0.5), 8.0))
    raise last_err

def parse_entry(jo_code, kaisai_bi, race_no):
    url = f"{BASE}/RaceList.do"
    params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no}
    soup = get_soup(url, params)
    title = soup.title.string if soup.title else ""
    race_name = ""
    m = re.search(r"【出走表】(.+?)｜", title or "")
    if m:
        race_name = m.group(1).strip()
    riders = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            text = row.get_text(" ", strip=True)
            car_m = re.search(r"\b([1-9])\b", text)
            if not car_m:
                continue
            car_no = car_m.group(1)
            name_m = re.search(r"([一-龥ぁ-んァ-ン　]{2,10})\s*(\d+)歳", text)
            name = name_m.group(1).replace("　", "").strip() if name_m else ""
            age = name_m.group(2) if name_m else ""
            period_m = re.search(r"(\d+)期", text)
            period = period_m.group(1) if period_m else ""
            pref_m = re.search(r"(北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)", text)
            pref = pref_m.group(1) if pref_m else ""
            class_m = re.search(r"(Ｓ[Ｓ１２]|Ａ[１２３]|Ｌ[１２])", text)
            klass = class_m.group(1) if class_m else ""
            style_m = re.search(r"(逃|追|両)", text)
            style = style_m.group(1) if style_m else ""
            score_m = re.search(r"競走得点[：:]\s*([\d.]+)", text)
            score = score_m.group(1) if score_m else ""
            if name and car_no:
                riders.append({"車番": car_no, "選手名": name, "年齢": age, "期": period, "地区": pref, "級班": klass, "脚質": style, "競走得点": score})
    seen = set()
    unique = []
    for r in riders:
        if r["車番"] not in seen and r["選手名"]:
            seen.add(r["車番"])
            unique.append(r)
    return {"race_name": race_name, "riders": sorted(unique, key=lambda x: int(x["車番"])), "url": f"{url}?joCode={jo_code}&kaisaiBi={kaisai_bi}&raceNo={race_no}"}

def _parse_raw_grid(soup):
    """
    オッズ表の生データを(列車番, 行車番, オッズ値)のリストとして抽出する。
    券種による意味づけ(1着/2着/順序の有無)は呼び出し側で行う。

    実ページ構造(2026年8月に実URLで確認):
    - 表の先頭行に列車番ヘッダー(1,2,3...)が並ぶ
    - 各データ行は先頭セルが行車番、以降は(オッズ値, 人気順位)のペアが
      列の順に並ぶ(該当なしの列は単に存在しない=詰めて並んでいる)
    """
    grid = []
    odds_table = None
    for table in soup.find_all("table"):
        if "odds" in (table.get("class") or []):
            odds_table = table
            break
    if odds_table is None:
        tables = soup.find_all("table", class_="tb51")
        if len(tables) >= 2:
            odds_table = tables[1]
    if not odds_table:
        return grid
    rows = odds_table.find_all("tr")
    if not rows:
        return grid

    header = []
    first_row_texts = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
    first_row_nums = [t for t in first_row_texts if re.fullmatch(r"\d+", t)]
    if len(first_row_nums) >= 2 and all(int(n) < 10 for n in first_row_nums):
        header = first_row_nums
    else:
        for row in rows:
            texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            nums = [t for t in texts if re.fullmatch(r"\d+", t)]
            if len(nums) >= 5 and all(int(n) < 10 for n in nums):
                header = nums
                break

    for row in rows:
        texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not texts or not re.fullmatch(r"\d+", texts[0]):
            continue
        row_car = texts[0]
        data_cells = texts[1:]
        numeric_vals = [t for t in data_cells if re.match(r"^[\d.]+$", t)]
        if numeric_vals and all(re.fullmatch(r"\d+", t) and int(t) < 10 for t in numeric_vals):
            continue  # ヘッダー行そのもの(車番の並びだけの行)は除外
        col_idx = 0
        i = 0
        while i < len(data_cells):
            t = data_cells[i]
            if re.match(r"^[\d.]+$", t):
                if col_idx < len(header):
                    col_car = header[col_idx]
                    if col_car != row_car:
                        grid.append({"col_car": col_car, "row_car": row_car, "odds": t})
                col_idx += 1
                i += 2  # (オッズ値, 人気順位)のペアで1列分。人気順位は読み飛ばす
            else:
                col_idx += 1
                i += 1
    return grid

def _expected_combo_count(bet_type, n_riders):
    if n_riders is None or n_riders < 2:
        return None
    n = n_riders
    if bet_type == "2車複":
        return math.comb(n, 2) if n >= 2 else 0
    if bet_type == "2車単":
        return n * (n - 1) if n >= 2 else 0
    if bet_type == "ワイド":
        return math.comb(n, 2) if n >= 2 else 0
    if bet_type == "3連複":
        return math.comb(n, 3) if n >= 3 else 0
    if bet_type == "3連単":
        return n * (n - 1) * (n - 2) if n >= 3 else 0
    return None

def _dedup_and_validate(bet_type, combos, n_riders):
    """
    combosは既に券種の意味に沿って組み立てられた
    [{"1着":.., "2着":.., ("3着":..)?, "オッズ":..}, ...] のリスト。
    重複除去(念のための保険)と、理論組み合わせ数との突合を行う。
    """
    unordered = bet_type in ("2車複", "ワイド", "3連複")
    dedup = {}
    for item in combos:
        if bet_type == "3連複":
            key = tuple(sorted([item["1着"], item["2着"], item["3着"]], key=int))
        elif unordered:
            key = tuple(sorted([item["1着"], item["2着"]], key=int))
        elif bet_type == "3連単":
            key = (item["1着"], item["2着"], item["3着"])
        else:  # 2車単
            key = (item["1着"], item["2着"])
        if key not in dedup:
            dedup[key] = item
    matrix = list(dedup.values())

    expected = _expected_combo_count(bet_type, n_riders)
    actual = len(matrix)
    is_complete = (expected is not None) and (actual >= expected)
    missing_count = max(0, expected - actual) if expected is not None else None
    return {
        "bet_type": bet_type,
        "matrix_count": actual,
        "expected_count": expected,
        "is_complete": is_complete,
        "missing_count": missing_count,
        "matrix": matrix,
    }

def parse_odds_simple(jo_code, kaisai_bi, race_no, bet_type, n_riders=None):
    """
    1ページで完結する券種向け(2車複=5, 2車単=6, ワイド=7)。
    - 2車複・ワイド: 車番の若い方/大きい方は順不同の組み合わせとして扱う
    - 2車単: 列車番=1着、行車番=2着(実ページで確認済みの向き)
    """
    url = f"{BASE}/Odds.do"
    params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no, "betType": bet_type}
    soup = get_soup(url, params, retries=4)
    grid = _parse_raw_grid(soup)
    name = BET_TYPES.get(bet_type, str(bet_type))
    combos = []
    for g in grid:
        if name == "2車単":
            combos.append({"1着": g["col_car"], "2着": g["row_car"], "オッズ": g["odds"]})
        else:  # 2車複・ワイド(順不同)
            combos.append({"1着": g["col_car"], "2着": g["row_car"], "オッズ": g["odds"]})
    result = _dedup_and_validate(name, combos, n_riders)
    result["bet_code"] = bet_type
    return result

def parse_odds_axis_based(jo_code, kaisai_bi, race_no, bet_type, n_riders=None, max_cars=9):
    """
    軸車番ごとにページが分かれている券種向け(3連複=8, 3連単=9)。
    軸車番(1〜9)ごとに並列取得し、失敗した軸だけ後でシリアルに追い取得する。
    """
    url = f"{BASE}/Odds.do"
    name = BET_TYPES.get(bet_type, str(bet_type))
    results_by_axis = {}

    def fetch_one(axis_car, retries=4):
        params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no, "betType": bet_type, "jikuCode": "1", "shaban": str(axis_car)}
        try:
            soup = get_soup(url, params, retries=retries)
            return _parse_raw_grid(soup)
        except Exception:
            return None

    cars_to_try = list(range(1, max_cars + 1))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_one, car): car for car in cars_to_try}
        for fut in as_completed(futs):
            car = futs[fut]
            results_by_axis[car] = fut.result()

    failed_axes = [c for c, g in results_by_axis.items() if g is None]
    for attempt in range(2):
        if not failed_axes:
            break
        still_failed = []
        for axis_car in failed_axes:
            time.sleep(0.5)
            g = fetch_one(axis_car, retries=3)
            if g is None:
                still_failed.append(axis_car)
            else:
                results_by_axis[axis_car] = g
        failed_axes = still_failed

    combos = []
    for axis_car, grid in results_by_axis.items():
        if not grid:
            continue
        for g in grid:
            if name == "3連単":
                combos.append({"1着": str(axis_car), "2着": g["col_car"], "3着": g["row_car"], "オッズ": g["odds"]})
            else:  # 3連複(順不同)
                combos.append({"1着": str(axis_car), "2着": g["col_car"], "3着": g["row_car"], "オッズ": g["odds"]})

    result = _dedup_and_validate(name, combos, n_riders)
    result["bet_code"] = bet_type
    result["failed_axes"] = sorted(failed_axes)
    return result

def parse_result(jo_code, kaisai_bi, race_no):
    url = f"{BASE}/RaceKekka.do"
    params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no}
    soup = get_soup(url, params, retries=4)
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = "".join(c.get_text(strip=True) for c in rows[0].find_all(["td", "th"]))
        if "着" not in header or "車番" not in header:
            continue
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 6 or not re.fullmatch(r"[1-9]", cells[0]):
                continue
            if int(cells[0]) > 3:
                continue
            results.append({"着順": cells[0], "車番": cells[1], "決まり手_着差": cells[5] if len(cells) > 5 else "", "上り": cells[6] if len(cells) > 6 else "", "SB": cells[7] if len(cells) > 7 else ""})
    text = soup.get_text(" ", strip=True)
    nums = re.findall(r"([\d,]+円)\((\d+)\)", text)
    labels = ["2車複", "ワイド", "ワイド", "ワイド", "3連複", "2車単", "3連単"]
    payouts = []
    for i, (yen, pop) in enumerate(nums):
        label = labels[i] if i < len(labels) else "その他"
        payouts.append({"賭式": label, "払戻金": yen, "人気": pop})
    return {"results": sorted(results, key=lambda x: int(x["着順"])), "payouts": payouts}

def scrape_one_race(jo_code, kaisai_bi, race_no):
    data = {
        "jo_code": jo_code, "kaisai_bi": kaisai_bi, "race_no": race_no,
        "scraped_at": datetime.now().isoformat(),
        "entry": {}, "odds": {}, "result": {},
    }

    for attempt in range(2):
        try:
            data["entry"] = parse_entry(jo_code, kaisai_bi, race_no)
            data.pop("entry_error", None)
            break
        except Exception as e:
            data["entry_error"] = str(e)
            data["entry"] = {}
            if attempt == 0:
                time.sleep(1.0)
    time.sleep(0.3)

    n_riders = len(data["entry"].get("riders", [])) if data.get("entry") else None

    for code in [5, 6, 7]:  # 2車複, 2車単, ワイド(1ページ完結)
        name = BET_TYPES[code]
        try:
            data["odds"][name] = parse_odds_simple(jo_code, kaisai_bi, race_no, code, n_riders=n_riders)
            time.sleep(0.3)
        except Exception as e:
            data["odds"][name] = {"error": str(e), "matrix_count": 0, "matrix": [], "is_complete": False}

    for code in [8, 9]:  # 3連複, 3連単(軸車番ごとに分割取得)
        name = BET_TYPES[code]
        try:
            data["odds"][name] = parse_odds_axis_based(jo_code, kaisai_bi, race_no, code, n_riders=n_riders)
            time.sleep(0.3)
        except Exception as e:
            data["odds"][name] = {"error": str(e), "matrix_count": 0, "matrix": [], "is_complete": False}

    for attempt in range(2):
        try:
            data["result"] = parse_result(jo_code, kaisai_bi, race_no)
            data.pop("result_error", None)
            break
        except Exception as e:
            data["result_error"] = str(e)
            data["result"] = {"results": [], "payouts": []}
            if attempt == 0:
                time.sleep(1.0)

    odds_complete = {bt: info.get("is_complete", False) for bt, info in data["odds"].items()}
    data["data_quality"] = {
        "entry_ok": bool(data.get("entry", {}).get("riders")),
        "result_ok": bool(data.get("result", {}).get("results")),
        "odds_complete": odds_complete,
        "all_complete": bool(data.get("entry", {}).get("riders")) and bool(data.get("result", {}).get("results")) and all(odds_complete.values()),
    }

    fname = f"{kaisai_bi}_{jo_code}_{race_no:02d}.json"
    path = os.path.join(OUTPUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data

if __name__ == "__main__":
    r = scrape_one_race("44", "20260818", 2)
    print("OK riders=", len(r["entry"].get("riders", [])), "quality=", r["data_quality"])
    for bt, info in r["odds"].items():
        print(" ", bt, info.get("matrix_count"), "/", info.get("expected_count"))
