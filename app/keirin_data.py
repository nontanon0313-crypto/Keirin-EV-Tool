"""
競輪場(開催場)と都道府県の対応表。
選手の「地区」(都道府県)と開催地を突き合わせて、地元選手かどうかを判定するために使う。
選手データの「地区」表記(例:千葉、岩手、福岡)と揃うよう、都道府県名で統一している。

全43場を収録(2026年時点の現行場)。表記ゆれ(例:「函館」「函館競輪場」等)にもある程度対応できるよう、
完全一致だけでなく前方一致でも照合する。
"""

VENUE_PREFECTURE = {
    "函館": "北海道",
    "青森": "青森",
    "いわき平": "福島",
    "弥彦": "新潟",
    "前橋": "群馬",
    "取手": "茨城",
    "宇都宮": "栃木",
    "大宮": "埼玉",
    "西武園": "埼玉",
    "京王閣": "東京",
    "立川": "東京",
    "松戸": "千葉",
    "千葉": "千葉",
    "花月園": "神奈川",
    "川崎": "神奈川",
    "平塚": "神奈川",
    "小田原": "神奈川",
    "伊東": "静岡",
    "静岡": "静岡",
    "名古屋": "愛知",
    "岐阜": "岐阜",
    "大垣": "岐阜",
    "豊橋": "愛知",
    "富山": "富山",
    "松阪": "三重",
    "四日市": "三重",
    "福井": "福井",
    "奈良": "奈良",
    "向日町": "京都",
    "近畿": "大阪",
    "岸和田": "大阪",
    "和歌山": "和歌山",
    "玉野": "岡山",
    "広島": "広島",
    "防府": "山口",
    "高松": "香川",
    "小松島": "徳島",
    "松山": "愛媛",
    "小倉": "福岡",
    "久留米": "福岡",
    "武雄": "佐賀",
    "佐世保": "長崎",
    "別府": "大分",
    "熊本": "熊本",
}


def get_prefecture_for_venue(venue_name: str) -> str | None:
    """開催場名から都道府県を推定する。前方一致・部分一致でも照合を試みる。"""
    if not venue_name:
        return None
    if venue_name in VENUE_PREFECTURE:
        return VENUE_PREFECTURE[venue_name]
    for name, pref in VENUE_PREFECTURE.items():
        if name in venue_name or venue_name in name:
            return pref
    return None


def is_local_player(venue_name: str, player_region: str) -> bool | None:
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
