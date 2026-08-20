
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odds Park 競輪 スクレイパー（耐障害版）
- 各リクエストにリトライ
- 3連単は1着軸ごとに独立取得（途中失敗しても継続）
- タイムアウト・接続エラーを握りつぶして可能な限りデータを残す
"""
import requests
from bs4 import BeautifulSoup
import json, time, re, os
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

_session = None

def get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session

def get_soup(url, params=None, retries=2):
    last_err = None
    for i in range(retries):
        try:
            r = get_session().get(url, params=params, timeout=(5, 10))
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last_err = e
            time.sleep(0.5 + i)
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

def _parse_odds_matrix(soup, fixed_first="1"):
    matrix = []
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
        return matrix
    rows = odds_table.find_all("tr")
    header_2 = []
    for row in rows:
        texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        nums = [t for t in texts if re.fullmatch(r"\d+", t)]
        if len(nums) >= 5 and all(int(n) < 10 for n in nums):
            header_2 = nums
            break
    for row in rows:
        texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if not texts or not re.fullmatch(r"\d+", texts[0]):
            continue
        third = texts[0]
        numeric_vals = [t for t in texts[1:] if re.match(r"^[\d.]+$", t)]
        if numeric_vals and all(re.fullmatch(r"\d+", t) and int(t) < 10 for t in numeric_vals):
            continue
        val_idx = 0
        for t in texts[1:]:
            if re.match(r"^[\d.]+$", t):
                if val_idx < len(header_2):
                    second = header_2[val_idx]
                    if second != third:
                        matrix.append({"1着": fixed_first, "2着": second, "3着": third, "オッズ": t})
                val_idx += 1
            else:
                val_idx += 1
    return matrix

def parse_odds_3rentan(jo_code, kaisai_bi, race_no, max_cars=9):
    """3連単を軸ごとに並列取得"""
    url = f"{BASE}/Odds.do"
    all_matrix = []

    def fetch_one(car):
        params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no, "betType": 9, "jikuCode": "1", "shaban": str(car)}
        try:
            soup = get_soup(url, params, retries=2)
            return _parse_odds_matrix(soup, fixed_first=str(car))
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_one, car): car for car in range(1, max_cars + 1)}
        for fut in as_completed(futs):
            matrix = fut.result()
            if matrix:
                all_matrix.extend(matrix)

    return {"bet_type": "3連単", "bet_code": 9, "matrix_count": len(all_matrix), "matrix": all_matrix}



def parse_odds_simple(jo_code, kaisai_bi, race_no, bet_type):
    url = f"{BASE}/Odds.do"
    params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no, "betType": bet_type}
    soup = get_soup(url, params, retries=1)
    matrix = _parse_odds_matrix(soup, fixed_first="1")
    return {"bet_type": BET_TYPES.get(bet_type, str(bet_type)), "bet_code": bet_type, "matrix_count": len(matrix), "matrix": matrix}

def parse_result(jo_code, kaisai_bi, race_no):
    url = f"{BASE}/RaceKekka.do"
    params = {"joCode": jo_code, "kaisaiBi": kaisai_bi, "raceNo": race_no}
    soup = get_soup(url, params, retries=1)
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
    try:
        data["entry"] = parse_entry(jo_code, kaisai_bi, race_no)
        time.sleep(0.3)
    except Exception as e:
        data["entry_error"] = str(e)

    # 3連単（全軸）
    try:
        data["odds"]["3連単"] = parse_odds_3rentan(jo_code, kaisai_bi, race_no)
        time.sleep(0.3)
    except Exception as e:
        data["odds"]["3連単"] = {"error": str(e), "matrix_count": 0, "matrix": []}

    for code in [6, 8, 5, 7]:  # 2車単, 3連複, 2車複, ワイド
        name = BET_TYPES[code]
        try:
            data["odds"][name] = parse_odds_simple(jo_code, kaisai_bi, race_no, code)
            time.sleep(0.3)
        except Exception as e:
            data["odds"][name] = {"error": str(e), "matrix_count": 0, "matrix": []}

    try:
        data["result"] = parse_result(jo_code, kaisai_bi, race_no)
    except Exception as e:
        data["result_error"] = str(e)
        data["result"] = {"results": [], "payouts": []}

    fname = f"{kaisai_bi}_{jo_code}_{race_no:02d}.json"
    path = os.path.join(OUTPUT_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data

if __name__ == "__main__":
    r = scrape_one_race("44", "20260818", 2)
    print("OK", r["odds"].get("3連単", {}).get("matrix_count"), len(r["entry"].get("riders", [])))
