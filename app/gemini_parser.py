"""
Gemini APIを使い、競輪アプリのスクリーンショットから構造化データを抽出する。

方針(開発ルールより):
- 特定アプリ(tipstar等)の固定フォーマットに依存しない汎用プロンプトにする
- 読み取れない項目はnullのまま許容する(無理に埋めない)
- source_app(読み取り元アプリ)が分かれば記録する
"""

import os
import json
import time
import base64
from typing import Optional
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# gemini-2.0-flash(2026/6廃止)→gemini-2.5-flash(新規ユーザー利用不可)と立て続けに使えなくなったため、
# 2026年8月時点で新規ユーザーも無料枠で使える正式版モデルに変更。
# 無料枠: 30 RPM / 1500 RPD、課金不要、画像入力対応(2026年8月確認)
MODEL_NAME = "gemini-3.1-flash-lite"

# 無料枠のレート制限(1分あたりのリクエスト数)に達した場合の自動リトライ設定
MAX_RETRIES = 3
DEFAULT_RETRY_WAIT_SECONDS = 20


def _call_with_retry(model, contents, generation_config):
    """無料枠のレート制限(429)に達した場合、少し待って自動リトライする。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return model.generate_content(contents, generation_config=generation_config)
        except ResourceExhausted as e:
            last_error = e
            wait_seconds = DEFAULT_RETRY_WAIT_SECONDS
            # エラーメッセージにretry_delayが含まれていれば、その秒数を優先して待つ
            try:
                if hasattr(e, "retry_delay") and e.retry_delay:
                    wait_seconds = e.retry_delay.seconds + 1
            except Exception:
                pass
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait_seconds)
    raise RuntimeError(
        f"Gemini APIの無料枠レート制限に達し、{MAX_RETRIES}回リトライしても失敗しました。"
        f"時間を空けて再度お試しください。詳細: {last_error}"
    )

# 画面の種類ごとに抽出してほしい項目を指示する共通プロンプト
EXTRACTION_PROMPT = """
あなたは競輪(自転車競技のギャンブル)アプリのスクリーンショットを解析するアシスタントです。
渡された画像から、以下のJSON形式で情報を抽出してください。

重要なルール:
- 特定のアプリの固定フォーマットを前提にせず、画面に表示されている内容から柔軟に読み取ってください
- 読み取れない項目は null にしてください。憶測で埋めないでください
- 数値は数値型で、パーセントは0-100のfloatで出力してください(例: 88.9% -> 88.9)
- 出力は有効なJSONのみ。説明文やMarkdownのコードブロック記号は付けないでください

出力するJSONスキーマ:
{
  "screen_type": "出走表基本情報 | 出走表直近成績 | 出走表勝率 | オッズ | 投票シート | 不明 のいずれか",
  "source_app": "アプリ名が推測できれば(例: tipstar)。不明ならnull",
  "venue_name": "競輪場名。不明ならnull",
  "race_number": "レース番号(整数)。不明ならnull",
  "grade": "GI/GII/GIII/F1/F2等。不明ならnull",
  "event_title": "開催名。不明ならnull",
  "deadline_time": "締切時刻の文字列(例: 16:54)。不明ならnull",
  "entries": [
    {
      "waku_number": "枠番(整数)",
      "car_number": "車番(整数)",
      "player_name": "選手名",
      "region": "地区",
      "player_class": "級班(例: L1)",
      "age": "年齢(整数)",
      "period": "期別",
      "evaluation_rank": "評価(クラウン等)",
      "race_score": "競走得点(float)",
      "leg_style": "脚質",
      "s_count": "S回数(整数)",
      "h_count": "H(逃げ)回数(整数)",
      "b_count": "B(まくり)回数(整数)",
      "kimarite_nige": "決まり手-逃(整数)",
      "kimarite_makuri": "決まり手-捲(整数)",
      "kimarite_sashi": "決まり手-差(整数)",
      "kimarite_mark": "決まり手-マーク(整数)",
      "finish_1st": "1着回数(整数)",
      "finish_2nd": "2着回数(整数)",
      "finish_3rd": "3着回数(整数)",
      "app_win_rate": "勝率%(float)",
      "app_2nd_rate": "2連対率%(float)",
      "app_3rd_rate": "3連対率%(float)",
      "gear_ratio": "ギア倍数(float)",
      "line_group": "ライン構成に関する情報があれば"
    }
  ],
  "odds_list": [
    {
      "bet_type": "単勝 | 複勝 | 2車単 | 2車複 | 2枠単 | 2枠複 | ワイド | 3連単 | 3連複",
      "combination": "車番の組み合わせ。着順ありは矢印区切りをハイフンに変換(例: 1→2→3 は 1-2-3)",
      "odds_value": "オッズ倍率(float)",
      "popularity_rank": "人気順位(整数)。分かれば"
    }
  ]
}

画面に写っていない項目のセクション(例: オッズ画面ならentriesが空)は、空配列にしてください。
"""


def _image_to_part(image_bytes: bytes, mime_type: str = "image/png"):
    return {"mime_type": mime_type, "data": image_bytes}


def parse_screenshot(image_bytes: bytes, mime_type: str = "image/png") -> dict:
    """
    1枚のスクリーンショットをGeminiに渡し、構造化JSONを取得する。
    呼び出し側で複数画像分の結果をマージしてDBに保存する想定。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません(Renderの環境変数を確認してください)")

    model = genai.GenerativeModel(MODEL_NAME)
    response = _call_with_retry(
        model,
        [EXTRACTION_PROMPT, _image_to_part(image_bytes, mime_type)],
        generation_config={"response_mime_type": "application/json"},
    )

    text = response.text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # まれにコードブロックが付く場合のフォールバック
        cleaned = text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)


def estimate_ai_win_probabilities(entries: list) -> dict:
    """
    選手データ一覧(競走得点・脚質・決まり手・着順分布・ライン構成等)をもとに、
    GeminiにAI独自の勝率推定をさせる。
    戻り値: {car_number: win_prob(0-1)} で、合計が1になるよう正規化されたもの。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません")

    prompt = f"""
以下は競輪レースの出走選手データです。各選手が1着になる確率を推定してください。
競走得点・脚質・S/H/B回数・決まり手の傾向・着順分布・ライン構成(分かれば)を総合的に考慮してください。
単一の要素(例:得点だけ)で機械的に決めず、複数の要素を組み合わせて判断してください。

出力は次のJSON形式のみ。説明文は不要です。確率の合計は1.0になるようにしてください。
{{"probabilities": {{"車番(文字列)": 確率(float 0-1), ...}}}}

選手データ:
{json.dumps(entries, ensure_ascii=False, indent=2)}
"""
    model = genai.GenerativeModel(MODEL_NAME)
    response = _call_with_retry(
        model,
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    text = response.text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned)

    probs = {int(k): float(v) for k, v in data.get("probabilities", {}).items()}
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs
