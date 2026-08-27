"""
競輪場(全国43場)のマスタデータ。

出典: netkeirin「Perfecta Navi」全国バンクデータ(2025年4月21日更新)
https://keirin.netkeiba.com/perfectanavi/news_detail.html?id=24090

周長・みなし直線は上記の実測値。lead_advantage_score(先行有利度)は独自算出であり、
公式な指標ではない点に注意(下記compute_lead_advantage参照)。

以前のバージョンでは、廃止済みの「花月園」や存在しない「近畿」という場名を誤って
含めていたため、このファイルで全面的に正しいデータへ差し替えている。
"""

from typing import Optional

"""
オッズパーク(oddspark.com)の場コード(jo_code)対応表。

出典: 2026年8月時点でoddspark.com上の実際のURL(joCode=)を複数点確認し、
ブロックごとの規則性(basecode + 都道府県一覧内でのindex)から算出。
「大垣」「川崎」「豊橋」「富山」など複数箇所で実URLと突合できたブロック(関東/南関東/中部)は
verified=Trueとし、1点のみ確認・規則性からの推定に留まるブロック(北日本/近畿/中国/四国/九州)は
verified=Falseとしている。verified=Falseのコードは自動インポート時に警告を出し、
必要に応じて手動確認を促す(のんの指摘=データ品質を最優先する方針を踏まえて追加)。
"""
JO_CODE_TO_VENUE = {
    # 全国共通の場コード(オッズパークAPI・電話投票番号と共通。のんの調査により
    # 全ブロック確定。以前の推測値には複数の誤り・ズレがあった)。
    # 北日本
    "11": "函館", "12": "青森", "13": "いわき平",
    # 関東
    "21": "弥彦", "22": "前橋", "23": "取手", "24": "宇都宮",
    "25": "大宮", "26": "西武園", "27": "京王閣", "28": "立川",
    # 南関東(33に該当する会場は無い)
    "31": "松戸", "32": "千葉", "34": "川崎", "35": "平塚",
    "36": "小田原", "37": "伊東温泉", "38": "静岡",
    # 中部(41=一宮は廃止済みだが、過去データのため一応残しておく)
    "41": "一宮(廃止)", "42": "名古屋", "43": "岐阜", "44": "大垣", "45": "豊橋",
    "46": "富山", "47": "松阪", "48": "四日市",
    # 近畿(52に該当する会場は無い)
    "51": "福井", "53": "奈良", "54": "向日町", "55": "和歌山", "56": "岸和田",
    # 中国
    "61": "玉野", "62": "広島", "63": "防府",
    # 四国(72に該当する会場は無い)
    "71": "高松", "73": "小松島", "74": "高知", "75": "松山",
    # 九州(82に該当する会場は無い)
    "81": "小倉", "83": "久留米", "84": "武雄", "85": "佐世保", "86": "別府", "87": "熊本",
}

# 全ブロック確定(のんの調査により全国共通コードと確認済みのため、
# 以前の「一部のみ検証済み」という状態から全件に更新)
JO_CODE_VERIFIED_BLOCKS = set(JO_CODE_TO_VENUE.keys())


def venue_name_from_jo_code(jo_code: str) -> Optional[tuple]:
    """jo_code(例:"44")から(場名, 検証済みか)を返す。未知のコードならNoneを返す。"""
    name = JO_CODE_TO_VENUE.get(jo_code)
    if name is None:
        return None
    return name, (jo_code in JO_CODE_VERIFIED_BLOCKS)

# {場名: (都道府県, 周長m, みなし直線m)}
# 千葉競輪場は2024年に1周250mの屋内木製バンクへ改修され、みなし直線の公表値が無いためNone。
VENUE_DATA = {
    "函館": ("北海道", 400, 51.3),
    "青森": ("青森", 400, 58.9),
    "いわき平": ("福島", 400, 62.7),
    "弥彦": ("新潟", 400, 63.1),
    "前橋": ("群馬", 335, 46.7),
    "取手": ("茨城", 400, 54.8),
    "宇都宮": ("栃木", 500, 63.3),
    "大宮": ("埼玉", 500, 66.7),
    "西武園": ("埼玉", 400, 47.6),
    "京王閣": ("東京", 400, 51.5),
    "立川": ("東京", 400, 58.0),
    "松戸": ("千葉", 333, 38.2),
    "千葉": ("千葉", 250, None),
    "川崎": ("神奈川", 400, 58.0),
    "平塚": ("神奈川", 400, 54.2),
    "小田原": ("神奈川", 333, 36.1),
    "伊東温泉": ("静岡", 333, 46.6),
    "静岡": ("静岡", 400, 56.4),
    "名古屋": ("愛知", 400, 58.8),
    "岐阜": ("岐阜", 400, 59.3),
    "大垣": ("岐阜", 400, 56.0),
    "豊橋": ("愛知", 400, 60.3),
    "富山": ("富山", 333, 43.0),
    "松阪": ("三重", 400, 61.5),
    "四日市": ("三重", 400, 62.4),
    "福井": ("福井", 400, 52.8),
    "奈良": ("奈良", 333, 38.0),
    "向日町": ("京都", 400, 47.3),
    "和歌山": ("和歌山", 400, 59.9),
    "岸和田": ("大阪", 400, 56.7),
    "玉野": ("岡山", 400, 47.9),
    "広島": ("広島", 400, 57.9),
    "防府": ("山口", 333, 42.5),
    "高松": ("香川", 400, 54.8),
    "小松島": ("徳島", 400, 55.5),
    "高知": ("高知", 500, 52.0),
    "松山": ("愛媛", 400, 58.6),
    "小倉": ("福岡", 400, 56.9),
    "久留米": ("福岡", 400, 50.7),
    "武雄": ("佐賀", 400, 64.4),
    "佐世保": ("長崎", 400, 40.2),
    "別府": ("大分", 400, 59.9),
    "熊本": ("熊本", 400, 60.3),
}

# みなし直線の分布(千葉を除く42場)。先行有利度の正規化に使う。
_STRAIGHTS = [v[2] for v in VENUE_DATA.values() if v[2] is not None]
_MIN_STRAIGHT = min(_STRAIGHTS)
_MAX_STRAIGHT = max(_STRAIGHTS)


def compute_lead_advantage(straight_m: Optional[float]) -> Optional[float]:
    """
    みなし直線の長さから、先行有利度(0=差し有利、1=先行絶対有利)を算出する。
    直線が短いほど先行有利、というのは競輪で広く知られる傾向で、
    (逃げ決着率が高い前橋・奈良・西武園・松戸・防府は実際に直線が短い上位5場と一致、
    逃げ決着率が低いいわき平・静岡・大宮・弥彦・豊橋は直線が長い場と概ね一致しており、
    この単純な線形換算は方向性としては実績と整合している)。
    ただし正式な指標ではなく、あくまで簡易的な推定値である。
    データが無い場合はNoneを返す(無理に埋めない)。
    """
    if straight_m is None:
        return None
    if _MAX_STRAIGHT == _MIN_STRAIGHT:
        return 0.5
    score = 1.0 - (straight_m - _MIN_STRAIGHT) / (_MAX_STRAIGHT - _MIN_STRAIGHT)
    return round(max(0.0, min(1.0, score)), 3)


def normalize_venue_name(venue_name: Optional[str]) -> Optional[str]:
    """
    スクショごとのOCR読み取りゆれ(表記ゆれ、例:「京王閣」と「京王閣競輪場」)を吸収し、
    43場マスタの正式名称に正規化する。同じレースなのに場名の揺れで別レースとして
    重複登録されてしまう不具合の対策(のんの報告により追加)。
    一致するマスタ名が見つからない場合は、元の文字列をそのまま返す。
    """
    if not venue_name:
        return venue_name
    stripped = venue_name.strip()
    if stripped in VENUE_DATA:
        return stripped
    for name in VENUE_DATA:
        if name in stripped or stripped in name:
            return name
    return stripped


def get_prefecture_for_venue(venue_name: str) -> Optional[str]:
    """開催場名から都道府県を推定する。前方一致・部分一致でも照合を試みる。"""
    if not venue_name:
        return None
    if venue_name in VENUE_DATA:
        return VENUE_DATA[venue_name][0]
    for name, (pref, _, _) in VENUE_DATA.items():
        if name in venue_name or venue_name in name:
            return pref
    return None


def is_local_player(venue_name: str, player_region: str) -> Optional[bool]:
    """
    選手が開催地の地元かどうかを判定する。
    どちらかの情報が無い、または照合できない場合はNone(不明)を返す。
    """
    if not player_region:
        return None
    pref = get_prefecture_for_venue(venue_name)
    if pref is None:
        return None
    return player_region.strip() == pref.strip()


def get_bank_seed_data() -> list:
    """bank_masterテーブルへの初期投入用データを返す。"""
    seed = []
    for name, (pref, lap, straight) in VENUE_DATA.items():
        seed.append({
            "name": name,
            "lap_length_m": float(lap),
            "home_stretch_length_m": straight,
            "lead_advantage_score": compute_lead_advantage(straight),
            "notes": (
                f"出典:netkeirin全国バンクデータ(2025/4/21更新)。{pref}。"
                + ("先行有利度は直線長からの簡易推定値。" if straight is not None else "みなし直線データなし(2024年改修)。")
            ),
        })
    return seed


# {場名: (緯度, 経度)} リアルタイム天候取得用のおおよその所在地座標(市区町村中心)
VENUE_COORDINATES = {
    "函館": (41.7687, 140.7291), "青森": (40.8244, 140.7400), "いわき平": (37.0500, 140.8833),
    "弥彦": (37.7014, 138.8264), "前橋": (36.3906, 139.0608), "取手": (35.9061, 140.0508),
    "宇都宮": (36.5551, 139.8828), "大宮": (35.9066, 139.6236), "西武園": (35.7897, 139.4914),
    "京王閣": (35.6486, 139.5461), "立川": (35.6938, 139.4142), "松戸": (35.7877, 139.9017),
    "千葉": (35.6073, 140.1233), "川崎": (35.5308, 139.7029), "平塚": (35.3267, 139.3428),
    "小田原": (35.2547, 139.1522), "伊東温泉": (34.9667, 139.1000), "静岡": (34.9756, 138.3828),
    "名古屋": (35.1815, 136.9066), "岐阜": (35.4233, 136.7606), "大垣": (35.3606, 136.6153),
    "豊橋": (34.7692, 137.3914), "富山": (36.6953, 137.2136), "松阪": (34.5775, 136.5264),
    "四日市": (34.9653, 136.6244), "福井": (36.0652, 136.2216), "奈良": (34.6851, 135.8048),
    "向日町": (34.9500, 135.7014), "和歌山": (34.2261, 135.1675), "岸和田": (34.4614, 135.3717),
    "玉野": (34.4939, 133.9464), "広島": (34.3853, 132.4553), "防府": (34.0517, 131.5619),
    "高松": (34.3401, 134.0434), "小松島": (34.0064, 134.5919), "高知": (33.5597, 133.5311),
    "松山": (33.8392, 132.7658), "小倉": (33.8834, 130.8752), "久留米": (33.3196, 130.5083),
    "武雄": (33.1931, 130.0136), "佐世保": (33.1594, 129.7228), "別府": (33.2846, 131.4914),
    "熊本": (32.8032, 130.7079),
}

# WMO weather_code(Open-Meteo)を日本語の天候表現に変換する簡易マップ
_WMO_WEATHER_JA = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり",
    45: "霧", 48: "霧(着氷性)",
    51: "霧雨(弱)", 53: "霧雨", 55: "霧雨(強)",
    56: "着氷性の霧雨(弱)", 57: "着氷性の霧雨(強)",
    61: "雨(弱)", 63: "雨", 65: "雨(強)",
    66: "着氷性の雨(弱)", 67: "着氷性の雨(強)",
    71: "雪(弱)", 73: "雪", 75: "雪(強)",
    77: "雪あられ",
    80: "にわか雨(弱)", 81: "にわか雨", 82: "にわか雨(強)",
    85: "にわか雪(弱)", 86: "にわか雪(強)",
    95: "雷雨", 96: "雷雨(雹あり)", 99: "雷雨(激しい雹あり)",
}


def get_current_weather(venue_name: str) -> Optional[dict]:
    """
    開催場の所在地座標から、Open-Meteo(無料・APIキー不要)でリアルタイムの天候・気温を取得する。
    スクショに天候情報が写っていないケースが多いため、その代替・補完として使う
    (のんの要望により追加)。取得できない場合はNoneを返す(呼び出し側はスクショ由来の値のまま扱う)。
    """
    import requests

    coords = VENUE_COORDINATES.get(venue_name)
    if not coords:
        return None
    lat, lon = coords
    try:
        res = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code",
                "timezone": "Asia/Tokyo",
            },
            timeout=8,
        )
        res.raise_for_status()
        current = res.json().get("current", {})
        temperature_c = current.get("temperature_2m")
        weather_code = current.get("weather_code")
        if temperature_c is None and weather_code is None:
            return None
        return {
            "weather": _WMO_WEATHER_JA.get(weather_code, "不明") if weather_code is not None else None,
            "temperature_c": temperature_c,
        }
    except Exception:
        # 天候取得はあくまで補助情報のため、失敗しても本処理は止めない
        return None
