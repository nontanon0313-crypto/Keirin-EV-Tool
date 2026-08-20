# 競輪データ取得ツール（Odds Park）

出走表・全軸3連単オッズ・他賭式オッズ・結果（1〜3着）・払戻を JSON で保存します。

## 必要環境

- Python 3.9+
- 依存: `pip install -r requirements.txt`

## 使い方

```bash
# 依存インストール
pip install -r requirements.txt

# 1レース取得
python run_keirin.py --date 20260818 --jo 44 --race 12

# 1日分（1〜12R）
python run_keirin.py --date 20260818 --jo 44 --all

# 複数日・未取得のみ
python run_keirin.py --dates 20260817,20260818 --jo 44 --all --skip-done
```

### 主なオプション

| オプション | 説明 |
|-----------|------|
| `--date YYYYMMDD` | 開催日（単日） |
| `--dates a,b,c` | 複数日 |
| `--jo` | 場コード（大垣=44） |
| `--race N` | レース番号 |
| `--races 1,2,12` | 複数レース |
| `--all` | 1〜12R |
| `--skip-done` | 既存JSONをスキップ |

### 出力

- 保存先: `data/`（環境変数 `KEIRIN_DATA_DIR` で変更可）
- ファイル名: `{YYYYMMDD}_{jo}_{RR}.json`
- ログ: `data/batch_log.txt`

### JSON に含まれる内容

- **entry**: 出走表（車番・選手名・級班・脚質・競走得点など）
- **odds**: 3連単（全1着軸）/ 2車単 / 3連複 / 2車複 / ワイド
- **result**: 1〜3着、払戻

## 注意

- Odds Park の公開ページを参照しています。アクセス間隔を空けていますが、過度な連続取得は避けてください。
- フルオッズは直近レースで取得しやすいです。古い開催ではオッズが残っていない場合があります。
- 場コード例: 大垣=44（他場は Odds Park の URL の `joCode` を確認）

## ファイル構成

```
keirin_scraper_pkg/
  keirin_oddspark_scraper.py  # 本体
  run_keirin.py               # CLI
  requirements.txt
  README.md
  data/                       # 出力先（実行後に生成）
```
