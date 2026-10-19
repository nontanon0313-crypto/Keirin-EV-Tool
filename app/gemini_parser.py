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

# 複数モデルをローテーション。無料枠(RPD/RPM)はモデル系統ごとに別枠のことが多い。
# 環境変数 GEMINI_MODEL_POOL=model1,model2 で上書き可。
_DEFAULT_POOL = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash-lite",
]
_env_pool = (os.environ.get("GEMINI_MODEL_POOL") or "").strip()
MODEL_CANDIDATES = [x.strip() for x in _env_pool.split(",") if x.strip()] or list(_DEFAULT_POOL)
MODEL_NAME = MODEL_CANDIDATES[0]  # 互換: 既定の先頭

# 同一モデル内の短時間リトライ
MAX_RETRIES = 2
DEFAULT_RETRY_WAIT_SECONDS = 15

# プロセス内でモデルを回す(429時に次へ)
_model_index = 0
_exhausted_models = set()


def _next_model_name(prefer: str = None) -> str:
    global _model_index
    n = len(MODEL_CANDIDATES)
    if n == 0:
        raise RuntimeError("Gemini モデル候補が空です")
    start = 0
    if prefer and prefer in MODEL_CANDIDATES:
        start = MODEL_CANDIDATES.index(prefer)
    for i in range(n):
        name = MODEL_CANDIDATES[(start + i) % n]
        if name not in _exhausted_models:
            _model_index = MODEL_CANDIDATES.index(name)
            return name
    # 全モデルが exhausted 扱いでも、日次枠リセットを期待して先頭から再試行
    _exhausted_models.clear()
    _model_index = 0
    return MODEL_CANDIDATES[0]


def _mark_exhausted(name: str) -> None:
    _exhausted_models.add(name)


def _call_with_retry(model, contents, generation_config, model_name: str = None):
    """同一モデルで短リトライ後、別モデルへフォールバックする。"""
    name = model_name or getattr(model, "model_name", None) or MODEL_NAME
    last_error = None
    tried = set()
    while True:
        if name in tried and len(tried) >= len(MODEL_CANDIDATES):
            break
        tried.add(name)
        active = model if (model_name is None and name == (getattr(model, "model_name", None) or MODEL_NAME)) else genai.GenerativeModel(name)
        for attempt in range(MAX_RETRIES):
            try:
                return active.generate_content(contents, generation_config=generation_config)
            except ResourceExhausted as e:
                last_error = e
                wait_seconds = DEFAULT_RETRY_WAIT_SECONDS
                try:
                    if hasattr(e, "retry_delay") and e.retry_delay:
                        wait_seconds = int(getattr(e.retry_delay, "seconds", 0) or 0) + 1
                except Exception:
                    pass
                if attempt < MAX_RETRIES - 1:
                    time.sleep(min(wait_seconds, 60))
                    continue
                _mark_exhausted(name)
                break
            except Exception as e:
                # モデル非対応・404 相当は次モデルへ
                msg = str(e).lower()
                last_error = e
                if any(k in msg for k in ("not found", "not supported", "invalid model", "404")):
                    _mark_exhausted(name)
                    break
                raise
        # 次のモデル
        prev = name
        name = _next_model_name(prefer=None)
        # prefer を進めたいので exhausted を避けた次を取る
        for cand in MODEL_CANDIDATES:
            if cand not in _exhausted_models and cand not in tried:
                name = cand
                break
        else:
            # 未試行が無い
            if len(tried) >= len(MODEL_CANDIDATES):
                break
            name = _next_model_name()
        if name == prev and name in _exhausted_models:
            # 進まないなら終了
            remaining = [c for c in MODEL_CANDIDATES if c not in tried]
            if not remaining:
                break
            name = remaining[0]
        model = genai.GenerativeModel(name)

    raise RuntimeError(
        f"Gemini APIの無料枠制限に達し、候補モデル {MODEL_CANDIDATES} でも失敗しました。最後のエラー: {last_error}"
    )


def _get_model():
    return genai.GenerativeModel(_next_model_name())



def _image_to_part(image_bytes: bytes, mime_type: str = "image/png"):
    return {"mime_type": mime_type, "data": image_bytes}


def parse_screenshot(image_bytes: bytes, mime_type: str = "image/png") -> dict:
    """
    1枚のスクリーンショットをGeminiに渡し、構造化JSONを取得する。
    呼び出し側で複数画像分の結果をマージしてDBに保存する想定。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません(Renderの環境変数を確認してください)")

    model = _get_model()
    response = _call_with_retry(
        model,
        [EXTRACTION_PROMPT, _image_to_part(image_bytes, mime_type)],
        # 旧: max_output_tokens=8192。オッズが100件超になるとJSON出力がここで途切れて
        # 不完全なJSONになり、フロント側で「Failed to fetch」的な失敗として現れていた。
        # gemini-3.1-flash-liteは65536まで対応しているため、余裕を持って32768に拡大。
        generation_config={"response_mime_type": "application/json", "max_output_tokens": 32768},
    )

    text = response.text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # まれにコードブロックが付く場合のフォールバック
        cleaned = text.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)


def _build_weather_text(weather_info: dict = None) -> str:
    """天候・気温・季節の情報を、戦術傾向の解説付きでプロンプト用テキストに変換する。"""
    if not weather_info:
        return ""
    weather = weather_info.get("weather")
    temperature_c = weather_info.get("temperature_c")
    season = weather_info.get("season")
    if not (weather or temperature_c is not None or season):
        return ""
    parts = []
    if weather:
        parts.append(f"天候:{weather}")
    if temperature_c is not None:
        parts.append(f"気温:{temperature_c}℃")
    if season:
        parts.append(f"季節:{season}")
    return (
        f"開催時の条件: {' / '.join(parts)}。\n"
        "(雨天・強風時は視界不良や風の抵抗により、後方から差す展開より前で仕掛ける先行選手が"
        "残りやすくなる傾向があります。逆に晴天・無風の好条件では、実力差がそのまま出やすく、"
        "追い込み選手にもチャンスが生まれやすくなります。低温時は筋肉の動きが硬くなりやすく、"
        "立ち上がりの鋭さが必要な捲り・差しにはやや不利、先行選手にはやや有利に働く傾向があります。"
        "これらはあくまで一般的な傾向であり、他の要素と組み合わせた参考情報として扱ってください)\n"
    )


def _is_girls_keirin(lines: list) -> bool:
    """
    ガールズケイリンかどうかを、ライン構成から推定する。
    ガールズケイリンは必ず全員単騎(ライン協調なし)になるため、
    ライン情報が全て1車ずつ(単騎)であれば、ガールズケイリンとみなせる
    (のんの指摘により追加。画面上に「ガールズ」等の明示が無くても判定できる)。
    """
    if not lines:
        return False
    return all(len(line) == 1 for line in lines)


def _girls_keirin_note(lines: list) -> str:
    if not _is_girls_keirin(lines):
        return ""
    return (
        "このレースは、ライン構成が全員単騎であることからガールズケイリンと推定されます。"
        "ガールズケイリンは男子と異なり、ライン(先行・番手・3番手の連携)による協調戦術が"
        "基本的に存在せず、各選手が個々の脚力・位置取りだけで戦う個人戦です。"
        "「番手選手が有利」「ラインの主導権争い」といった、ライン協調を前提とした推論は"
        "当てはめず、各選手の脚質・競走得点・位置取りの巧拙を中心に判断してください。\n"
    )


def simulate_race_development(
    entries: list, lines: list = None, bank_info: dict = None,
    race_stage: str = None, grade: str = None, weather_info: dict = None,
) -> str:
    """
    レース展開を予想する(1段階目)。
    「誰が先行し、誰が番手・追い込みに回るか」「どのラインが主導権を握るか」
    「展開のポイント」を、AIに文章として言語化させる。
    数値シミュレーションではなく、言語による展開予想にすることで、
    AIが根拠を持って一貫した推論をしやすくし、かつ人間にも読める形で残す。
    戻り値: 展開予想の文章(そのままDBに保存し、後段の勝率推定にも渡す)。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません")

    lines_text = (
        f"ライン構成: {json.dumps(lines, ensure_ascii=False)}\n(各配列が1ライン、先行→番手→3番手の順)\n"
        if lines
        else (
            "ライン情報: 不明(今回は取得できていません)。\n"
            "ライン(並び)が分からない前提で予想してください。特定の選手を強い本命として"
            "断定的に書かず、「並び次第で複数の可能性がある」ことが分かるトーンで書いてください。\n"
        )
    )
    lines_text += _girls_keirin_note(lines)
    bank_text = ""
    if bank_info and bank_info.get("lead_advantage_score") is not None:
        bank_text = (
            f"バンク特性: 周長{bank_info.get('lap_length_m')}m、みなし直線{bank_info.get('home_stretch_length_m')}m、"
            f"先行有利度{bank_info.get('lead_advantage_score')}(0=差し有利〜1=先行絶対有利)\n"
        )
    stage_text = f"グレード:{grade or '不明'} / ステージ:{race_stage or '不明'}\n" if (race_stage or grade) else ""
    weather_text = _build_weather_text(weather_info)

    prompt = f"""
あなたは競輪の展開予想の専門家です。以下の情報から、このレースがどのように展開するかを予想してください。

{lines_text}{bank_text}{stage_text}{weather_text}
選手データ:
{json.dumps(entries, ensure_ascii=False, indent=2)}

以下の観点を含めて、300字程度で予想してください:
- どのライン(先行選手)が主導権を握りそうか
- 各ラインの位置取り(先行/番手/差し/後方)の予想
- 展開のポイント(誰かが仕掛けるタイミング、警戒される選手等)
- この展開の場合、有利になりやすい選手・不利になりやすい選手

出力は予想文章のみ。JSON形式にせず、日本語の文章でそのまま出力してください。
"""
    model = _get_model()
    response = _call_with_retry(model, prompt, generation_config={})
    return response.text.strip()


def estimate_ai_win_probabilities(
    entries: list, lines: list = None, bank_info: dict = None,
    race_stage: str = None, grade: str = None, development_simulation: str = None,
    weather_info: dict = None,
) -> dict:
    """
    選手データ一覧(競走得点・脚質・決まり手・着順分布・ライン構成等)をもとに、
    GeminiにAI独自の勝率推定をさせる。

    tipstar等の app_win_rate があれば entries に含めてよい(ライン・決まり手と同様の参考値)。
    最終勝率はAIが単独で確立する(後段での重み付け合成は行わない)。

    lines: [[1,2],[3],[4,5,6],[7]]形式のライン構成(分かれば)。
    bank_info: {"lap_length_m":..,"home_stretch_length_m":..,"lead_advantage_score":..}形式のバンク特性(分かれば)。
    race_stage: 予選/準決勝/決勝等のステージ(分かれば)。grade: GI/GII等(分かれば)。
    development_simulation: simulate_race_developmentで生成した展開予想の文章(分かれば)。
    weather_info: {"weather":..,"temperature_c":..,"season":..}形式の開催時条件(分かれば)。
    戻り値: {car_number: win_prob(0-1)} で、合計が1になるよう正規化されたもの。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が設定されていません")

    lines_text = (
        f"ライン構成: {json.dumps(lines, ensure_ascii=False)}\n"
        "(各配列が1ライン。配列内は先行→番手→3番手の順。競輪では同ラインの選手同士が"
        "連携して走り、先行選手が番手の選手のために展開を作るため、同ラインの選手が"
        "上位を占めやすい傾向があります。ただし、これは「同ラインなら誰でも一律に有利」という"
        "単純な話ではなく、そのライン内の先行選手(特に1番手)の実力が高いほど、"
        "ライン全体が上位に来る可能性が上がります。ライン構成と各選手の実力(競走得点・脚質等)を"
        "掛け合わせて判断してください。弱いライン(先行選手の実力が低い)を過大評価しないでください)\n"
        if lines else (
            "ライン構成: 不明(今回は取得できていません)。\n"
            "重要: 競輪の着順は、選手個人の実力(競走得点等)だけでなく、当日の並び(どの選手が"
            "どの選手を先行させ、誰の番手に付くか)という「あなたが観測できない情報」に"
            "大きく左右される競技です。実際、並び予想を欠いた状態でAIが特定の選手を強く"
            "本命視すると、その選手が実際には想定した並びの恩恵を受けられず、期待したほど"
            "勝てないことが多いと分かっています。したがって、ライン情報が無い今回のようなケースでは:\n"
            "  - 競走得点や級班が高いというだけで、特定の1〜2名に確率を強く集中させないこと\n"
            "  - 「明確に他を上回る材料が無い限り、実力上位の数名に確率を分散させておく」ことを既定の姿勢とすること\n"
            "  - 1着確率が突出して高い(例:30%を超える)選手を作る場合は、それだけの根拠"
            "(圧倒的な競走得点差、地元開催、当地区の実績等、選手データから明確に読み取れる材料)を"
            "自分の中で明確に説明できる場合に限ること\n"
        )
    )
    lines_text += _girls_keirin_note(lines)

    bank_text = ""
    if bank_info and bank_info.get("lead_advantage_score") is not None:
        bank_text = (
            f"開催バンクの特性: 周長{bank_info.get('lap_length_m')}m、"
            f"みなし直線{bank_info.get('home_stretch_length_m')}m、"
            f"先行有利度スコア{bank_info.get('lead_advantage_score')}(0=差し有利〜1=先行絶対有利)。\n"
            "(直線が短いバンクほど先行選手が残りやすく、直線が長いバンクほど差し・追い込みが決まりやすい"
            "傾向があります。この先行有利度スコアと、各選手の脚質(逃/追込/両)を掛け合わせて、"
            "脚質がバンク特性に合っている選手を評価に反映してください)\n"
        )

    stage_text = ""
    if race_stage or grade:
        stage_text = (
            f"このレースの格: グレード={grade or '不明'}、ステージ={race_stage or '不明'}。\n"
            "(予選・二次予選等の早い段階のレースでは、上位進出さえできればよいため、選手が"
            "無理に勝ちを狙わず着順だけを意識した保守的な走りをすることがあります。"
            "一方、準決勝・決勝や、初日特選のような格の高いレースほど、全員が全力で"
            "勝ちを狙う「ガチ度」が上がる傾向があります。過去の着順・決まり手の実績が"
            "どのステージで記録されたものかは分からない前提で、この点は参考程度に留めてください)\n"
        )

    development_text = (
        f"展開予想(事前に別途生成したもの): {development_simulation}\n"
        "(この展開予想を踏まえて、実際に上位に来やすい選手を評価してください。"
        "展開予想と矛盾する確率(例:番手に回る予想の選手を、先行選手より高く評価する等)を"
        "つけないよう注意してください)\n"
        if development_simulation else ""
    )
    weather_text = _build_weather_text(weather_info)

    prompt = f"""
以下は競輪レースの出走選手データです。各選手が1着になる確率を推定してください。
競走得点・脚質・S/H/B回数・決まり手の傾向・着順分布を総合的に考慮してください。
"pre_race_comment"に前検コメント等があれば、調子や仕上がり具合の参考情報として考慮してください。
"region"(地区)がある場合、開催地との近さ(隣接地区か等)も参考情報として考慮してください。
{lines_text}{bank_text}{stage_text}{weather_text}{development_text}"is_local"がtrueの選手は、その開催地の地元選手であることを意味します。地元選手は声援を受けて
好走しやすい傾向が一般に知られているため、他条件が拮抗している場合のプラス要因として考慮してください
(ただし地元であること単体を過大評価せず、あくまで複数要素の一つとして扱ってください)。
単一の要素(例:得点だけ)で機械的に決めず、複数の要素を組み合わせて判断してください。
特に次を必ず考慮してください(欠けている場合はその旨を織り込む):
- ライン構成(先行・番手の関係。同ラインは連携しやすく、先頭が残ると番手も上位に来やすい)
- 決まり手の傾向(逃・捲・差・マの回数)
- S/H/B回数、脚質、競走得点、直近着順
- バンク特性(先行有利度)と脚質の相性
- ステージ(予選/準決勝/決勝等)とグレード
確率は過大評価しがちなので、本命でも過信せず、実力差が小さい場合は確率を均し気味にしてください。
アプリ側の勝率(tipstar等)がある場合は参考情報として扱い、最終的な勝率はあなた自身の判断で単独に決めてください(コピーしないこと)。
あなた自身の判断だけによる、独立した推定を行ってください。

出力は次のJSON形式のみ。説明文は不要です。確率の合計は1.0になるようにしてください。
{{"probabilities": {{"車番(文字列)": 確率(float 0-1), ...}}}}

選手データ:
{json.dumps(entries, ensure_ascii=False, indent=2)}
"""
    model = _get_model()
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
