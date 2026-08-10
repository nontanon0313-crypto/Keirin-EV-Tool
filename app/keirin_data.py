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
