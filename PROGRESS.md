# Keirin-EV-Tool 進捗・引き継ぎメモ

このファイルは、Claude・Grok・ChatGPT どれが作業する場合でも
「今どこまで進んでいて、何が既知の問題で、次に何をすべきか」をすぐ把握するための
引き継ぎドキュメントです。

## 【運用ルール・必読・厳守】

**このファイルは「追記」ではなく「上書き」が原則。** 過去の履歴を積み増すと
肥大化し、誰も全部読まなくなって引き継ぎが機能しなくなる(2026-09-05に発覚した問題)。

1. 「1. 現在の状況」は**常に最新1件だけ**を残す。前の内容は消してよい。
   古い検証結果を積み増しで残さない。
2. 課題が解決したら「4. 未解決の課題」から**削除する**。今後の役に立つ
   技術的知見だけを一言で「6. 技術的な教訓」に移す。
3. 日付ごとの詳細な実験結果・具体的な数値の羅列は本体に書かず、
   `PROGRESS_ARCHIVE.md` に追記する(こちらは追記でよい・肥大化OK)。
   本体(このファイル)は常に短く保つこと。
4. 作業を始めるときは「1. 現在の状況」だけを読めば足りるようにする。
   2章以降は仕様・ルールのリファレンスであり、毎回読む必要はない。
5. 作業を終えるとき・区切りがついたときは、必ず「1. 現在の状況」を
   その時点の内容に**書き換える**。「最終更新」の日付と担当
   (Claude/Grok/ChatGPT)も必ず書き換える。書き換えを忘れた状態で
   セッションを終えない。
6. 大きな設計変更や新しい発見があれば、該当する章(2〜6)も更新する。

---

## 1. 現在の状況(★これだけ読めば足りる・毎回上書きする)

最終更新: 2026-09-05(Grok) — 方針Bを300-1000倍帯にも拡張

### フェーズ
1000-3000倍は方針Bでほぼ解消(n=71→1)。直近の主戦場は300-1000倍(n≈255)で
ratio0.44・想定ROI過大傾向。同方式を300-1000倍にも適用。

### 実装
- `HIGH_ODDS_BANDS` = 300-1000 / 1000-3000 / 3000以上
- 的中率残差で確率縮小のみ（禁止しない）

### 次
デプロイ → warm → `run_replay_continue.sh 50 2` → `run_pva_recent.sh 3 50`
確認: 300-1000のpredROI低下、ratioが極端に崩れないこと

## 2. プロジェクト概要

競輪のEV(期待値)ベース投票分析ツール。Android/Termux専用(PC不使用)、
インフラは全て無料プランで構成。**実資金投票はまだ開始していない**
(予想精度がまだ目安に届いていないため)。

- API: https://keirin-ev-tool.onrender.com (Render無料プラン)
- DB: Neon(主系・DATABASE_URL) + Supabase(副系・DATABASE_URL_FALLBACK)
- GitHub: https://github.com/nontanon0313-crypto/Keirin-EV-Tool.git
- フロントエンド: バニラJS PWA (GitHub Pages)
- AI予想: Gemini API
- スクレイピング: oddspark.com、結果確定後に収集(投票締切とのタイミングは無関係)

開発体制: Grok / Claude / ChatGPT が交代で対応。ユーザー本人はコマンドを叩く役、
git push はユーザーが実行。

### 投票ロジックの設計(現行)

```
出走表・オッズ取得(結果確定後にスクレイピング)
  → AI予想(Gemini、blended_win_prob)
  → 候補生成: オッズが存在する組み合わせを全て評価(box等の絞り込みは無い)
  → 確率補正 第1段: get_calibration_factors_retroactive
      (Purchase/SkippedBetの記録に頼らず、確定済みレース全件・オッズ全件を
       毎回使う偏りの無い方法。券種×勝率帯 → 勝率帯 → 全体 の優先順位)
  → 確率補正 第2段: get_purchase_set_calibration_factors
      (実購入集合だけに残る「選択後の楽観バイアス(勝者の呪い)」への残差補正。
       券種×オッズ帯優先、係数は0.15〜1.5にクランプ)
  → EV計算(p×odds-1)
  → 運用ゲート:
      - ステージゲート: 着順指定券種(3連単・2車単)はそのステージの確定済み
        サンプルが30件未満なら一律見送り
      - 実績ゲート: ステージ別・券種別の全期間実績収支率が損益分岐(0%)以下
        (サンプル50件以上)なら丸ごと見送り
  → ポートフォリオ最適化(_select_portfolio): 固定ケリー額でガミり制約を
     満たしつつ期待利益最大になるよう選定。選ばれなかった買い示唆は
     「ポートフォリオ最適化で選外」として見送り記録に残る
  → 見送り記録: 期待値マイナスでも券種ごとに確率上位30件は検証用に記録
    (NEGATIVE_EV_VERIFICATION_TOP_N=30)
```

**キャリブレーションの経緯(重要な教訓)**: 当初はPurchase/SkippedBetの記録から
係数を学習していたが、「期待値マイナスの組み合わせを記録しない」仕様だったため
偏った自己参照的な状態になっていた。偏りの無い遡及検証方式
(`get_calibration_factors_retroactive`)に切り替え(2026-09-01)、現行係数は
0.95倍前後(ほぼ無補正)で妥当と判明。ただし「勝者の呪い」的な選択バイアスは
母集団レベルの補正だけでは消えないため、実購入集合の残差を補正する第2段を追加。
**見送り記録の方針(何を記録し何を記録しないか)が精度検証全体の生命線。**

---

## 3. 主要エンドポイント一覧

### 投票・確定
- `POST /ev/race-plan/{race_id}` — 投票プラン生成(本番ロジック本体)
- `POST /races/{race_id}/confirm-result` — 結果確定・的中判定
- `POST /races/{race_id}/replay-settled` — 過去レースをrace_plan+confirmで再生(検証用)
- `POST /races/replay-settled/batch`, `GET /races/replay-settled/targets`

### キャリブレーション・診断
- `POST /purchases/warm-calibration` — 校正係数・ゲート集計を事前計算してキャッシュ
- `GET /purchases/calibration-factors-compare` — 現行係数 vs 遡及検証係数の比較
- `GET /purchases/retroactive-capture-diagnostics` — 記録に頼らない捕捉率・Brier検証
- `GET /purchases/bet-type-diagnostics?since=calibration_switch` — 券種別5段階診断
  (候補生成→ランキング→購入フィルタ→確率→収益化。sinceで補正切替後だけに絞り込み可)
- `GET /purchases/investment-readiness?since=calibration_switch` — 実資金投入の目安判定
- `GET /purchases/diagnostics/predicted-vs-actual-return` — 想定ROI/実績ROIの乖離。
  `hours` / `last_n_races` で直近窓だけに絞り込み可(校正判定は必ずこちらを使う)
- `GET /purchase-diagnostics/winning-capture` — 的中買い目がpurchase/skipped/not_recordedの
  どれに落ちたかの一括集計(まだreplayしていない直近レースはnot_recordedで埋まるのが正常)
- `GET /purchase-diagnostics/*` — その他多数の診断(ev-bands, raw-vs-calibrated,
  filter-effectiveness, odds-drift, stage-gate, race-plan-design 等。詳細は
  `app/routers/purchase_diagnostics.py`参照)

### DB運用
- `POST /admin-sync/...` — Neon⇔Supabase差分同期をHTTP経由で実行(Render上でシェルが
  使えないため)。実体は `scraper/sync_neon_to_supabase.py` / `sync_supabase_to_neon.py`

### よく使うコマンド
```bash
# 直近校正の確認(これだけ見ればよい)
bash "$HOME/Keirin-EV-Tool/scraper/run_pva_recent.sh" 6 100

# 必要なら少数replay(大量は不要)
bash "$HOME/Keirin-EV-Tool/scraper/run_replay_continue.sh" 50 2

# ヘルスチェック
python3 -c "import requests,json; print(json.dumps(requests.get('https://keirin-ev-tool.onrender.com/health',timeout=60).json(),ensure_ascii=False,indent=2))"
```

---

## 4. 未解決の課題(現存するもののみ・解決したら削除する)

### 4.1 line_boost=1.2が未検証の固定値
`app/ev_calculator.py`の同ライン車ブースト倍率が「実績データに基づく検証待ちの
暫定パラメータ」とコード内に明記されている。3連単・2車単・3連複(順序を当てる
必要がある券種)はランキング精度が低い傾向があり、この暫定値が原因の一つである
可能性がある。提案: Harville本体は変更せず、遡及検証エンドポイントにline_boost
を差し替えて計算し直すオプションを追加し、1.0/1.2/1.5等で比較する。**未着手。**

### 4.2 未来リーク(as-of校正)が未実装
`get_calibration_factors_retroactive`・`get_purchase_set_calibration_factors`は
確定済み全データを見て係数を計算するため、replay時に「そのレース時点より未来の
実績」が係数に含まれる可能性がある。本格的なバックテストには、race_dateより前の
データだけで係数を計算する「as-of」方式が必要。現状は参考値として有効だが、
真の予測性能とは区別して扱うこと。

### 4.3 選択ロジックの設計方針
現行は期待利益最大化(`_value = stake*(p*odds-1)`)でポートフォリオを選ぶ。
確率校正を直しても、この指標がEV最大のままだと高オッズ偏重に戻りうる。
ワイド中心・的中率寄りの方針への転換は検討中。**勝手に閾値を変えないこと。
設計合意してから着手する。**

### 4.4 Supabase↔Neon同期
停止期間の書き込みギャップが未同期。優先度低・後回し可。

### 4.5 winning-captureの母集団
未replayの高race_idを見て100% not_recordedになる。診断の母集団指定を直すと
よいが必須ではない。

### 4.6 retroactive-capture-diagnosticsの移行日指定で500エラー
`GET /purchases/retroactive-capture-diagnostics`にキャリブレーション移行日
(2026-09-01)を指定すると500エラーになる問題を調査中。parse_actual_result・
確率計算・キャリブレーションの例外を個別に捕捉してエラー箇所を切り分ける修正を
実装・構文検証済みだが未デプロイ。次はデプロイ後に実際のエラー詳細を取得して
原因箇所を特定する。

---

## 0. 最優先作業ルール（必須・例外なし）

- **「進め」「続行」「全部進め」等の指示を受けた場合、途中で作業を止めない。**
  次にユーザーによる実行・認証・判断が必要になる地点まで、可能な作業を自律的に連続して進める。
- 「確認しました」「次は〜です」「ここで待ちます」等の中間報告だけで作業を終了してはならない。
- 原則として、
  **現行ソース確認 → 原因特定 → 最小修正 → 検証 → 診断実行 → 結果分析 → 必要な追加修正 → 再検証**
  の順に、実行可能なところまで連続して進める。
- 次に行う作業が明確な場合、ユーザーへの不要な確認質問を挟まない。
- ユーザーが実行・認証・判断しなければ進められない地点に到達した場合のみ、そこで停止して必要な操作を明示する。
- **PROGRESS.mdをKeirin-EV-Toolの作業ルールの正本とする。**
- **「進め」「続行」「全部進め」等を受けた作業では、作業開始時に必ずこの「0. 最優先作業ルール」を確認し、ユーザーの実行・認証・判断が必要になるまで自律的に連続実行する。**
  - 途中の調査・分析・修正・検証が完了しただけでは停止しない。
  - 「次に○○します」「次は○○です」「確認しました」「ここで待ちます」等を中間地点で終了条件にしない。
  - 次工程が明確なら、そのまま次工程へ進む。
  - ユーザーへコマンドを提示して結果待ちになる地点だけを停止地点とする。
  - 次のユーザー入力として再度「進め」を要求してはならない。
 会話上の一時的な指示よりも、この章に記載された必須ルールを優先して継続的に適用する。
- **ユーザーに提示する実行コマンドへ `git diff`、`git diff --check`、`git diff ...` 等の差分表示コマンドを絶対に含めない。** 差分確認が必要な場合は作業側で確認し、ユーザーには検証・commit・push等の必要なコマンドだけを提示する。
- 作業の区切り・完了時には、必ずPROGRESS.mdの「1. 現在の状況」をその時点の最新状態へ更新する。

---

## 5. 運用ルール・やってはいけないこと

- **Harville本体(`combination_prob`/`wide_prob`/`harville_prob`)を推測で
  全面的に書き換えない。** 変更する場合は実データで検証してから。
- **`is_complete`フラグや係数を手動でTrue/改ざんしない。**
- **負のEVを積極的に買う方向への変更はしない**(既に再計算で負は正当と確認済み)。
- **選択ロジックの閾値(EV最大化 vs 的中率重視など)を勝手に変えない。**
  設計合意してから。
- git操作(push)はユーザーが実行する。Claude/Grokはzipまたはパッチで返す。
- **git commitとgit pushのコマンドは、実行状態を考慮して書くこと。**
  - `git commit`が既に成功している場合、次のコマンドは`git push origin main`だけを書く。
  - `git commit && git push`を、既にコミット済みの状態で再実行しない。
    `git commit`が「nothing to commit」で終了コード1になると`&&`により`git push`まで実行されないため。
  - 修正→commit→pushを一度に提示する場合は、commit対象を明示し、commit成功後にpushまで実行される構成にする。
  - ユーザーがcommit済みの実行結果を提示した場合は、commitを再実行せず、pushだけを提示する。
- 既存実装を確認した上で最小差分。推測での大規模改修はしない。
- デプロイコマンドは実際に動く形で書く(プレースホルダー禁止)。
  リポジトリのローカルパスは `~/Keirin-EV-Tool`、zipのダウンロード先は
  `~/storage/downloads`。
  型:
  ```bash
  cd ~/Keirin-EV-Tool && \
  unzip -o ~/storage/downloads/<zip名> -d . && \
  git add <変更ファイル> && \
  git commit -m "<内容>" && \
  git push && \
  rm -f ~/storage/downloads/<今回のzip>
  ```
- コマンドはできるだけ1行・スクリプト化。bankrollは検証時1000000を明示。
- 実資金投票は精度が目安に届くまで開始しない
  (investment-readinessの4基準: サンプル数・統計的有意性・実績収支率黒字・
  破産確率10%以下)。
- スクリプト実行コマンド(replay_settled.py等)を提示する際は、必ず
  `cd ~/Keirin-EV-Tool &&` を先頭に明記する(単体で書くとFileNotFoundになる)。

---

## 6. 技術的な教訓(解決済みだが今後のために残す知見)

- **race-plan遅延の原因**: `confirm_race_result`がPurchase/SkippedBetを
  ORM属性代入→個別UPDATE文で更新していたため、200件規模で40〜55秒かかっていた。
  `bulk_update_mappings`も行ごとにUPDATEを発行するため効果なし、かつORM属性を
  dirtyにしたまま使うと二重更新になる。`UPDATE ... FROM (VALUES ...)`のSQL1本に
  変更し、`race_id`にIndexを追加して解消(数秒以下に短縮)。
- **Render無料プランのメモリ制限でプロセスが再起動している説は否定済み**
  (50レースreplayでPID・稼働時間が単調増加のまま完走を確認)。
- `Purchase`モデルには`created_at`列が無く、購入日時は`purchased_at`。
  (`Race`・`SkippedBet`・`EvResult`には`created_at`がある。命名の不統一に注意)
- Neonの接続URLに`channel_binding=require`が付いているとpsycopg2で接続失敗
  することがある(database.py側で除去済み)。
- FastAPI(Starlette)は通常GETに対しHEADも自動処理するはずだが、実機で
  `HEAD /`が405になりRenderのヘルスチェックが失敗する事象が発生。
  `@app.head("/")`を明示追加して解消済み。
- Render無料+Cloudflareのレート制限で、500件連続replayの後半にHTTP 429が
  多発することがある。アプリ側の問題ではなくインフラ側の制限。レース間隔・
  429指数バックオフリトライを`replay_settled.py`に実装済み。
- 全体(全期間)のratioは古い購入が支配して動かない。校正の良し悪しは必ず
  `predicted-vs-actual-return`の`hours`/`last_n_races`で直近窓だけ見て判定する。

---

## 7. 過去の詳細な検証ログ

日付ごとの具体的な実験結果・数値の推移は `PROGRESS_ARCHIVE.md` を参照。
(2026-09-04時点までの校正調整の経緯・券種別/オッズ帯別の詳細な数値はそちらに移動済み)
