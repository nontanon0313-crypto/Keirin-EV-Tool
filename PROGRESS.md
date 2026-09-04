# Keirin-EV-Tool 進捗・引き継ぎメモ

このファイルは、Claude・Grokどちらが作業する場合でも「今どこまで進んでいて、
何が既知の問題で、次に何をすべきか」をすぐ把握できるようにするための
引き継ぎドキュメントです。**大きな変更を加えたら、このファイルも更新してください。**
(のんの要望: セッションが変わる・利用制限に達する・別のAIが作業する、といった
状況でも引き継ぎが途切れないようにするため 2026-09-04 に導入)

最終更新: 2026-09-04(Grok) — confirm VALUES1本化 + Index import修正

---

## 1. プロジェクト概要

競輪のEV(期待値)ベース投票分析ツール。Android/Termux専用(PC不使用)、
インフラは全て無料プランで構成。**実資金投票はまだ開始していない**
(予想精度がまだ目安に届いていないため)。

- API: https://keirin-ev-tool.onrender.com (Render無料プラン)
- DB: Neon(主系・DATABASE_URL) + Supabase(副系・DATABASE_URL_FALLBACK)
- GitHub: https://github.com/nontanon0313-crypto/Keirin-EV-Tool.git
- フロントエンド: バニラJS PWA (GitHub Pages)
- AI予想: Gemini API
- スクレイピング: oddspark.com、結果確定後に収集(投票締切とのタイミングは無関係)

開発体制: Grok(Termux上での実行・一次修正)とClaude(コードレビュー・設計・
統合)の分業。ユーザー本人はコマンドを叩く役、git push はユーザーが実行。

---

## 2. 現在の投票ロジックの設計(2026-09-04時点)

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
      - 実績ゲート: ステージ別・券種別の全期間実績収支率が-50%以下
        (サンプル50件以上)なら丸ごと見送り
  → ポートフォリオ最適化(_select_portfolio): 固定ケリー額でガミり制約を
     満たしつつ期待利益最大になるよう選定。選ばれなかった買い示唆は
     「ポートフォリオ最適化で選外」として見送り記録に残る
  → 見送り記録: 期待値マイナスでも券種ごとに確率上位30件は検証用に記録
    (NEGATIVE_EV_VERIFICATION_TOP_N=30)
```

### キャリブレーションの経緯(重要な教訓)

- 当初、補正係数はPurchase/SkippedBetの記録から学習していたが、
  「期待値マイナスの組み合わせを記録しない」仕様だったため、
  **偏った(選ばれた後の)サンプルで学習・評価する自己参照的な状態**になっていた。
- 偏りの無い方法(`get_calibration_factors_retroactive`)で再計算したところ、
  現行係数(0.4〜0.7倍)は過剰な下方修正で、本来は0.95倍前後(ほぼ無補正)で
  十分だったことが判明。2026-09-01に本番ロジックを切り替え済み。
- ただし「勝者の呪い」的な選択バイアスは母集団レベルの補正だけでは消えない
  ため、実購入集合だけに残る残差を補正する第2段(purchase_set_factor)を追加。
- **教訓**: 見送り記録の方針(何を記録し、何を記録しないか)が、後々の
  キャリブレーション・診断全てに影響する。記録の網羅性は精度検証の生命線。

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
- `GET /purchases/diagnostics/predicted-vs-actual-return` — 想定ROI/実績ROIの乖離
- `GET /purchase-diagnostics/winning-capture` — 的中買い目がpurchase/skipped/not_recordedの
  どれに落ちたかの一括集計(直近の確定済みレースをid降順で見るため、
  **まだreplayしていない直近レースはnot_recordedで埋まるのが正常**。バグではない)
- `GET /purchase-diagnostics/*` — その他多数の診断(ev-bands, raw-vs-calibrated,
  filter-effectiveness, odds-drift, stage-gate, race-plan-design 等。詳細は
  `app/routers/purchase_diagnostics.py`参照)

### DB運用
- `POST /admin-sync/...` — Neon⇔Supabase差分同期をHTTP経由で実行(Render上でシェルが
  使えないため)。実体は `scraper/sync_neon_to_supabase.py` / `sync_supabase_to_neon.py`

---

## 4. 未解決・調査中の課題(優先順)

### 4.1 race-planが1レース約45〜54秒かかる件 → 原因判明、修正実装中(2026-09-04)

**原因を特定した。** `race_plan`自体は2.4〜2.6秒と高速(校正キャッシュが正しく効いている)。
真の犯人は`app/routers/races.py`の`confirm_race_result`だった。

計測結果(5レース分、race_plan_call_totalは全て2.4〜2.6秒):

| race_id | confirm_race_result | skipped_saved件数 |
|---|---|---|
| 344 | 45.53s | 179 |
| 345 | 45.84s | 181 |
| 346 | 48.76s | 192 |
| 347 | 54.31s | 218 |
| 348 | 49.12s | 195 |

**原因**: `confirm_race_result`はPurchase・SkippedBetを1件ずつPythonオブジェクトの
属性として書き換え、最後に`db.commit()`を1回呼ぶ実装だった。これは「1回のcommit」に
見えるが、SQLAlchemyの通常のORMオブジェクト更新は**変更された行の数だけ個別のUPDATE文を
送信する**ため、実質的にSkippedBet 180〜220件分の個別ネットワーク往復が発生していた。
1件あたり約230ミリ秒(Neonとの往復)と考えると、200件×0.23秒≒46秒とほぼ一致する。

**対応1 (Claude, 効果なし)**: `bulk_update_mappings` に置き換えたが、実測で
confirm は 47〜56秒のまま。仮説「1件ずつのUPDATE が原因」だけでは不十分だった。

**真因 (Grok, 2026-09-04 再検証)**:
1. SQLAlchemy の `bulk_update_mappings` は **行ごとに UPDATE を発行**する
   （1本のSQLにはならない）。Neon 遠距離では 1往復~200ms × 200件 ≒ 40秒。
2. さらに ORM オブジェクトへ `p.result = ...` と代入したままだったため、
   `commit()` 時にユニット・オブ・ワークが **同じ更新を二重発行**していた。

**対応2 (今回)**:
- ORM 属性は触らない（dirty にしない）
- `UPDATE ... FROM (VALUES ...)` の **SQL 1本**で Purchase / SkippedBet を更新
- `purchases.race_id` / `skipped_bets.race_id` に Index を追加

**次にやること（必須）**:
```bash
cd ~/Keirin-EV-Tool
python3 -u scraper/replay_settled.py --since all --limit 5 --bankroll 1000000
```
`confirm_race_result` が **数秒以下** になっているか確認。なっていれば
50件→全件 replay を再開する。

**教訓**:
- `bulk_update_mappings` ≠ 1往復。遠距離DBでは VALUES/UNNEST の1文UPDATEが必要。
- ORM を dirty にしたまま bulk すると二重更新になる。

### 4.2 line_boost=1.2が未検証の固定値(保留中・未着手)
- `app/ev_calculator.py`の`line_map_from_race`(または対応する現行コード)で、
  同ラインの車を1.2倍にブーストする処理があるが、この倍率は
  「実績データに基づく検証待ちの暫定パラメータ」とコード内に明記されている。
- 3連単・2車単・3連複(順序を当てる必要がある券種)は、ワイド・2車複より
  ランキング精度(捕捉後のTop1的中率)が低い傾向があり、この暫定値が原因の
  一つである可能性がある(Harville式の多段階計算で誤差が積み重なるため)。
- 提案していた対応: Harville本体は変更せず、遡及検証エンドポイントに
  line_boostを差し替えて計算し直すオプションを追加し、1.0/1.2/1.5等で
  ランキング精度がどう変わるか比較する。**この実装はまだ着手していない。**

### 4.3 未来リーク(as-of校正)が未実装
- `get_calibration_factors_retroactive`・`get_purchase_set_calibration_factors`は
  確定済み全データを見て係数を計算するため、replay時に「そのレース時点より
  未来の実績」が係数に含まれる可能性がある。
- 本格的なバックテストとして正しく評価するには、race_dateより前のデータだけで
  係数を計算する「as-of」方式が必要。現状は「今のモデルなら過去レース全体で
  どれくらいの性能か」という参考値としては有効だが、真の予測性能とは区別して
  扱うこと。

### 4.4 選択ロジックの設計方針(閾値変更は設計合意してから)
- 現行は期待利益最大化(`_value = stake*(p*odds-1)`)でポートフォリオを選ぶ。
- 確率校正を直しても、この指標がEV最大のままだと高オッズ偏重に戻りうる。
- ワイド中心・的中率寄りの方針への転換は、のんが検討中。**勝手に閾値を
  変えないこと。設計合意してから着手する。**

### 4.5 predicted-vs-actual-returnの現在値(2026-09-03時点)
- n=2180、想定的中率/実績的中率の比 = 1.87倍(目標: 1.3倍以内)
- 想定ROI/実績ROI の比 = 1.96倍(目標: 2倍以内。**ほぼ目標達成**)
- 母数がまだ小さいので、replayを増やしてから再確認すること。

---

## 5. 運用ルール・やってはいけないこと

- **Harville本体(`combination_prob`/`wide_prob`/`harville_prob`)を推測で
  全面的に書き換えない。** 変更する場合は実データで検証してから。
- **`is_complete`フラグや係数を手動でTrue/改ざんしない。**
- **負のEVを積極的に買う方向への変更はしない**(既に再計算で負は正当と確認済み)。
- **選択ロジックの閾値(EV最大化 vs 的中率重視など)を勝手に変えない。**
  設計合意してから。
- git操作(push)はユーザーが実行する。Claude/Grokはzipまたはパッチで返す。
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
  git push
  ```
- コマンドはできるだけ1行・スクリプト化。bankrollは検証時1000000を明示。
- 実資金投票は精度が目安に届くまで開始しない
  (investment-readinessの4基準: サンプル数・統計的有意性・実績収支率黒字・
  破産確率10%以下)。

---

## 6. Claude側で判明した技術的な注意点

- **Render無料プランのメモリ制限でプロセスが再起動している説は否定済み**
  (2026-09-03、50レースreplayでPID・稼働時間が単調増加のまま完走を確認)。
  race-plan遅延の原因はメモリ/プロセス再起動ではなく、DB書き込みか
  何らかのループ内処理と考えられる(4.1参照、調査中)。
- `Purchase`モデルには`created_at`列が無く、購入日時は`purchased_at`。
  (`Race`・`SkippedBet`・`EvResult`には`created_at`がある。命名の不統一に注意)
- Neonの接続URLに`channel_binding=require`が付いているとpsycopg2で接続
  失敗することがある(database.py側で除去済み)。
- `database.py`の`get_db()`は`DATABASE_PREFER`の優先順で毎回試すため、
  主系(Neon)が不調な間は毎リクエストで主系への接続タイムアウト分の
  遅延が乗る可能性がある(まだ実測はしていない)。
- FastAPI(Starlette)は通常GETルートに対してHEADも自動処理するはずだが、
  実機で`HEAD /`が405になりRenderのヘルスチェックが失敗、デプロイが
  終わらなくなる事象が発生。`@app.head("/")`を明示追加して解消済み。

---

## 7. 次にやるべきこと(優先順)

1. **4.1のtimings計測結果を確認する**(5件replayを実行し、`timings:`行を貼る)
2. 上記の結果に基づき、race-planの遅延を実際に解消する
3. 4.2(line_boost検証)に着手する
4. replayの母数を増やし、predicted-vs-actual-returnの比率(1.3倍・2倍以内)を
   再確認する
5. 4.4(選択ロジックの設計)について、のんと方針をすり合わせる

---

## 8. このファイルの更新ルール

- 大きな設計変更・新しい発見・解決した課題があれば、このファイルの該当箇所を
  更新すること。
- 「最終更新」の日付と担当(Claude/Grok)を必ず書き換えること。
- 課題を解決したら「4. 未解決・調査中の課題」から削除し、必要なら
  「6. 技術的な注意点」に教訓として残すこと。
