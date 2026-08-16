// バックエンドのURLは固定(このプロジェクト専用のRenderデプロイ先)
const API_BASE_URL = "https://keirin-ev-tool.onrender.com";
// フラクショナルケリー係数は固定運用(通常変更不要)
const FIXED_KELLY_COEFFICIENT = 0.25;

function apiUrl(path) {
  return API_BASE_URL + path;
}

document.getElementById("rebateCheckbox").addEventListener("change", (e) => {
  document.getElementById("rebatePctWrapper").style.display = e.target.checked ? "block" : "none";
});

document.getElementById("suggestMarginBtn").addEventListener("click", async () => {
  try {
    const res = await fetch(apiUrl("/purchases/suggested-margin"));
    const data = await res.json();
    document.getElementById("minEvInput").value = data.suggested_margin_pct;
    alert(data.reason);
  } catch (e) {
    alert("エラー: " + e.message);
  }
});

function getRebatePct() {
  const checked = document.getElementById("rebateCheckbox").checked;
  if (!checked) return 0;
  return (parseFloat(document.getElementById("rebatePctInput").value) || 0) / 100;
}

// ---------- 💰 証拠金管理 ----------
async function refreshBankrollDisplay() {
  const box = document.getElementById("bankrollDisplay");
  box.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/bankroll/"));
    const data = await res.json();
    if (!data.initialized) {
      box.textContent = "未設定です。下から初期額を設定してください。";
      return;
    }
    box.textContent = `現在の残高: ${data.current_balance}円 (初期設定額: ${data.initial_balance}円)`;
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
}

document.getElementById("bankrollSetBtn").addEventListener("click", async () => {
  const val = parseFloat(document.getElementById("bankrollSetInput").value);
  if (!val || val <= 0) {
    alert("正しい金額を入力してください");
    return;
  }
  if (!confirm(`証拠金を${val}円に設定(上書き)します。よろしいですか？`)) return;
  try {
    const res = await fetch(apiUrl("/bankroll/set"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_balance: val }),
    });
    if (!res.ok) throw new Error(await res.text());
    await refreshBankrollDisplay();
  } catch (e) {
    alert("エラー: " + e.message);
  }
});

// Renderの無料プランはアクセスが無いと自動スリープし、次のアクセスで起動に約1分かかる。
// 起動完了を待ってから本処理を送るためのウォームアップ関数。
async function wakeUpBackend(statusCallback) {
  const maxAttempts = 20; // 約60秒待つ(3秒間隔×20回)
  let lastError = null;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const res = await fetch(apiUrl("/health"), { cache: "no-store" });
      if (res.ok) {
        const data = await res.json();
        if (data && data.status === "healthy") {
          return true;
        }
      } else {
        lastError = `HTTPステータス ${res.status}`;
      }
    } catch (e) {
      lastError = e.message || String(e);
    }
    if (statusCallback) {
      statusCallback(`サーバー起動待ち...(${i + 1}/${maxAttempts})\n無料プランはアクセスが無いとスリープするため、初回は最大1分ほどかかります。${lastError ? "\n直近のエラー: " + lastError : ""}`);
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  if (statusCallback) {
    statusCallback(`サーバーが応答しません。\nAPIベースURL: ${API_BASE_URL}\n直近のエラー: ${lastError || "不明"}`);
  }
  return false;
}

// ---------- ① スクショ解析 ----------
document.getElementById("uploadBtn").addEventListener("click", async () => {
  const input = document.getElementById("screenshotInput");
  const resultBox = document.getElementById("uploadResult");
  if (!input.files.length) {
    alert("画像を選択してください");
    return;
  }

  resultBox.textContent = "サーバーの状態を確認中...";
  const awake = await wakeUpBackend((msg) => { resultBox.textContent = msg; });
  if (!awake) {
    return;
  }

  const isFinalOdds = document.getElementById("isFinalOddsCheckbox").checked;
  const files = Array.from(input.files);
  const total = files.length;
  let doneCount = 0;
  const logLines = [];

  function renderProgress() {
    resultBox.textContent = `解析中... (${doneCount}/${total}枚完了)\n` + logLines.join("\n");
  }
  // 更新のたびにブラウザへ描画の機会を渡す。これをしないと、複数の更新がほぼ同時に
  // 起きた場合にブラウザが描画をまとめてしまい、途中経過が画面に一度も表示されないまま
  // 最後の状態だけが見える、という不具合が起きうる(のんの報告により追加)。
  async function renderProgressAndFlush() {
    renderProgress();
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  }
  await renderProgressAndFlush();

  const formData = new FormData();
  for (const file of files) formData.append("files", file);
  formData.append("is_final_odds", isFinalOdds);

  // サーバー側は全ファイルを並列処理しつつ、完了したものから順に1行ずつ(NDJSON)返す。
  // これにより、並列処理の速さと「今どこまで進んだか」の進捗表示を両立している
  // (以前は1枚ずつ個別リクエストにして進捗を見せていたが、並列度が下がっていた)。
  let summary = null;
  try {
    const res = await fetch(apiUrl("/analyze/screenshots"), { method: "POST", body: formData });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `サーバーエラー(${res.status})`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // 未完成の最終行は次回に持ち越す
      for (const line of lines) {
        if (!line.trim()) continue;
        const obj = JSON.parse(line);
        if (obj.type === "summary") {
          summary = obj;
          continue;
        }
        logLines.push(obj.error
          ? `- ${obj.filename}: ❌解析失敗(${obj.error})`
          : `- ${obj.filename}: ${obj.screen_type} (レースID:${obj.race_id} 選手${obj.entries_found}件 オッズ${obj.odds_found}件)`);
        doneCount++;
        await renderProgressAndFlush();
      }
    }
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message + "\n" + logLines.join("\n");
    return;
  }

  const errorCount = summary?.error_count ?? 0;
  const finalOddsUpdatedTotal = summary?.final_odds_updated_count ?? 0;
  const raceIdsSeen = summary?.race_ids ?? [];

  resultBox.textContent = `解析完了: ${total}枚処理${errorCount ? `(うち${errorCount}枚は解析失敗)` : ""}${finalOddsUpdatedTotal ? `\n最終オッズを${finalOddsUpdatedTotal}件の購入履歴に反映しました` : ""}\n` +
    logLines.join("\n");
  // 今回反映したレースが1つならそれを、複数なら最後のものを自動選択して詳細まで表示する
  const newRaceId = raceIdsSeen.length ? raceIdsSeen[raceIdsSeen.length - 1] : null;
  await loadRaces(newRaceId);
});

// ---------- ② レース選択・期待値計算 ----------
async function loadRaces(selectRaceId) {
  const select = document.getElementById("raceSelect");
  const previousValue = select.value;
  try {
    const res = await fetch(apiUrl("/races/"));
    const races = await res.json();
    select.innerHTML = races.map(r =>
      `<option value="${r.id}">${r.race_date ? r.race_date + " " : ""}${r.venue_name} ${r.race_number}R (選手${r.entry_count}/オッズ${r.odds_count})</option>`
    ).join("");
    // アップロード直後は今回反映したレースを、それ以外は元々選ばれていたレースを維持する
    const target = selectRaceId ?? previousValue;
    if (target && races.some(r => String(r.id) === String(target))) {
      select.value = target;
    }
    await checkRace();
  } catch (e) {
    console.error(e);
  }
}


document.getElementById("deleteRaceBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  if (!raceId) {
    alert("削除するレースを選択してください(投票タブでレースを選んでから戻ってきてください)");
    return;
  }
  const select = document.getElementById("raceSelect");
  const label = select.options[select.selectedIndex] ? select.options[select.selectedIndex].text : raceId;
  if (!confirm(`「${label}」を削除します。よろしいですか？(元に戻せません)`)) {
    return;
  }
  try {
    const res = await fetch(apiUrl(`/races/${raceId}`), { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    alert("削除しました");
    await loadRaces();
  } catch (e) {
    alert("削除エラー: " + e.message);
  }
});

document.getElementById("deleteAllBtn").addEventListener("click", async () => {
  if (!confirm("レース・購入履歴・見送り記録など全ての記録を削除します。証拠金残高は削除されません。本当によろしいですか？(元に戻せません)")) {
    return;
  }
  if (!confirm("最終確認です。本当に全記録を削除しますか？")) {
    return;
  }
  try {
    const res = await fetch(apiUrl("/races/"), { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    alert("全ての記録を削除しました");
    await loadRaces();
  } catch (e) {
    alert("削除エラー: " + e.message);
  }
});

async function checkRace() {
  const raceId = document.getElementById("raceSelect").value;
  const box = document.getElementById("raceDetailResult");
  if (!raceId) {
    box.textContent = "";
    return;
  }
  box.textContent = "確認中...";
  try {
    const res = await fetch(apiUrl(`/races/${raceId}`));
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    let html = `<p><strong>${data.venue_name} ${data.race_number}R</strong> - `;
    html += data.ready_for_ev_calc
      ? `<span class="ev-positive">期待値計算に必要なデータは揃っています</span></p>`
      : `<span style="color:#ef4444;">まだ不足しているデータがあります(選手データ・オッズに加えて「AI予想を実行」が必要な場合があります)</span></p>`;

    if (data.lines_data && data.lines_data.length) {
      html += `<p>ライン構成: ${data.lines_data.map(l => l.join("-")).join(" / ")}</p>`;
    } else {
      html += `<p style="color:#94a3b8;">ライン構成: 未取得(「並び予想」画面等を読み込ませると反映されます)</p>`;
    }

    if (data.bank_info && data.bank_info.lead_advantage_score !== null) {
      const b = data.bank_info;
      html += `<p>バンク特性: 周長${b.lap_length_m}m / みなし直線${b.home_stretch_length_m}m / 先行有利度${b.lead_advantage_score}(0=差し有利〜1=先行絶対有利)</p>`;
    } else {
      html += `<p style="color:#94a3b8;">バンク特性: データなし</p>`;
    }

    html += `<p>グレード: ${data.grade ?? "不明"} / ステージ: ${data.race_stage ?? "不明"}</p>`;
    html += `<p>季節: ${data.season ?? "不明"} / 天候: ${data.weather ?? "データなし"}${data.temperature_c !== null ? ` / 気温${data.temperature_c}℃` : ""}</p>`;

    if (data.development_simulation) {
      html += `<div style="background:#0b1220;border-radius:8px;padding:10px;margin-top:8px;"><strong>🎯AIによる展開予想</strong><p style="white-space:pre-wrap;margin-top:6px;">${data.development_simulation}</p></div>`;
    }

    html += `<table><tr><th>車番</th><th>選手名</th><th>地区</th><th>脚質</th><th>アプリ勝率</th><th>AI推定</th><th>合成勝率</th></tr>`;
    for (const e of data.entries) {
      const status = e.ready_for_ev ? "" : ' style="color:#ef4444;"';
      const appWinRateLabel = `${e.app_win_rate ?? "-"}${e.zero_app_win_rate_warning ? " ⚠️欠場でないか要確認" : ""}`;
      html += `<tr${status}><td>${e.car_number}</td><td>${e.player_name}</td><td>${e.region ?? "-"}</td><td>${e.leg_style ?? "-"}</td><td>${appWinRateLabel}</td><td>${e.ai_win_prob ?? "-"}</td><td>${e.blended_win_prob ?? "未取得"}</td></tr>`;
    }
    html += `</table>`;

    html += `<p>オッズ件数: ${data.odds_count}件</p><ul>`;
    if (Object.keys(data.odds_by_type).length === 0) {
      html += `<li style="color:#ef4444;">オッズが1件も読み込まれていません</li>`;
    } else {
      for (const [betType, count] of Object.entries(data.odds_by_type)) {
        html += `<li>${betType}: ${count}件</li>`;
      }
    }
    html += `</ul>`;

    box.innerHTML = html;
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
}
document.getElementById("raceSelect").addEventListener("change", checkRace);
document.getElementById("refreshRaceListBtn").addEventListener("click", () => loadRaces());

function getBankrollOverride() {
  // 証拠金は常に証拠金タブの残高を使う(のんの要望により上書き欄を廃止)
  return null;
}

document.getElementById("racePlanBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  const resultBox = document.getElementById("evResult");
  if (!raceId) {
    alert("レースを選択してください");
    return;
  }
  const bankroll = getBankrollOverride();
  const kellyCoef = FIXED_KELLY_COEFFICIENT;
  const minProb = parseFloat(document.getElementById("minProbInput").value) / 100;
  const minEvPct = parseFloat(document.getElementById("minEvInput").value);

  resultBox.textContent = "サーバーの状態を確認中...";
  const awake = await wakeUpBackend((msg) => { resultBox.textContent = msg; });
  if (!awake) {
    return;
  }

  // AI予想(展開予想+勝率推定)がまだ済んでいなければ、先に自動で実行する
  // (以前は別ボタンだったが、のんの要望で自動投票プラン作成に統合)
  try {
    const raceRes = await fetch(apiUrl(`/races/${raceId}`));
    const raceData = await raceRes.json();
    const needsEstimate = !raceData.entries || raceData.entries.some((e) => !e.ready_for_ev);
    if (needsEstimate) {
      resultBox.textContent = "AI予想を実行中...(展開予想→勝率推定の2段階、数十秒かかります)";
      const estRes = await fetch(apiUrl(`/analyze/estimate/${raceId}`), { method: "POST" });
      const estData = await estRes.json();
      if (!estRes.ok) throw new Error(estData.detail || "AI予想の実行に失敗しました");
      // ②の詳細表示(AI推定・合成勝率の列)を更新する
      // (以前は「AI予想を実行」ボタン自身がこれを呼んでいたが、ボタン統合で漏れていた)
      await checkRace();
    }
  } catch (e) {
    resultBox.textContent = "エラー(AI予想): " + e.message;
    return;
  }

  resultBox.textContent = "プラン作成中...";
  try {
    const res = await fetch(apiUrl(`/ev/race-plan/${raceId}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        race_id: parseInt(raceId),
        bankroll,
        fractional_coefficient: kellyCoef,
        min_win_prob: minProb,
        min_ev_pct: minEvPct,
        max_race_pct: parseFloat(document.getElementById("maxRacePctInput").value) / 100,
        rebate_pct: getRebatePct(),
        max_items: parseInt(document.getElementById("maxItemsInput").value) || 20,
        exclude_low_prob_warning: document.getElementById("excludeLowProbCheckbox").checked,
        avoid_garami: document.getElementById("avoidGaramiCheckbox").checked,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    if (!data.items || data.items.length === 0) {
      resultBox.textContent = data.message || "買い示唆がありませんでした(見送り推奨)";
      return;
    }

    let html = `<p><strong>合計投票額: ${data.total_stake}円</strong>(上限${data.race_budget_cap}円)`;
    html += `<br><span class="note">大穴帯除外設定: ${data.exclude_low_prob_warning_requested ? "ON" : "OFF"}(除外件数${data.excluded_low_prob_count}件)</span>`;
    if (data.excluded_by_min_stake_count > 0) html += `<br>理論上の賭け金が最低単位(100円)未満のため${data.excluded_by_min_stake_count}件を見送りました`;
    if (data.excluded_by_garami_count > 0) html += `<br>ガミり回避のため${data.excluded_by_garami_count}件を除外しました`;
    if (data.excluded_by_budget_count > 0) html += `<br>予算の都合で${data.excluded_by_budget_count}件は見送りました(期待値が低い順に除外)`;
    if (data.garami_free) {
      html += `<br>✅この組み合わせは、的中すれば必ず合計投票額以上を回収できます(ガミりなし保証)`;
      const margins = data.odds_safety_margins_used_pct || {};
      const marginEntries = Object.entries(margins);
      if (marginEntries.length > 0) {
        html += `<br><span class="note">券種別オッズ安全マージン: ${marginEntries.map(([bt, m]) => `${bt} -${m}%`).join(" / ")}(実績データ不足の券種はデフォルト-${20}%を使用)</span>`;
      }
    }
    html += `</p>`;
    html += `<p>レース全体の回収率: ${data.race_roi_pct}%(期待利益 約${data.total_expected_profit}円) / レース全体の的中率: 約${data.race_hit_prob_pct}%</p>`;
    html += `<table><tr><th>券種</th><th>買い目</th><th>勝率</th><th>オッズ</th><th>回収率%</th><th>投票額</th></tr>`;
    for (const it of data.items) {
      const probLabel = `${it.estimated_win_prob_pct}%${it.low_prob_warning ? " ⚠️低確率帯(未補正)" : ""}`;
      html += `<tr class="ev-positive"><td>${it.bet_type}</td><td>${it.combination}</td><td>${probLabel}</td><td>${it.odds_value}</td><td>${it.roi_pct}%</td><td>${it.stake}円</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;

    // 「まとめて購入記録する」ボタンを動的に追加(このプランの内容を保持しておく)
    lastRacePlan = { raceId: parseInt(raceId), items: data.items };
    const bulkBtn = document.createElement("button");
    bulkBtn.textContent = `この${data.items.length}点をまとめて購入記録する`;
    bulkBtn.style.background = "#f59e0b";
    bulkBtn.addEventListener("click", recordRacePlanAsPurchases);
    resultBox.appendChild(bulkBtn);
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

let lastRacePlan = null;

document.getElementById("thresholdTableBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  const resultBox = document.getElementById("evResult");
  if (!raceId) {
    alert("レースを選択してください");
    return;
  }
  const minProb = parseFloat(document.getElementById("minProbInput").value) / 100;
  const minEvPct = parseFloat(document.getElementById("minEvInput").value);
  const limit = parseInt(document.getElementById("thresholdLimitInput").value) || 15;

  resultBox.textContent = "閾値表を作成中...";
  try {
    const res = await fetch(apiUrl(`/ev/threshold-table/${raceId}?min_ev_pct=${minEvPct}&min_win_prob=${minProb}&limit=${limit}&rebate_pct=${getRebatePct()}`), {
      method: "POST",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    let html = `<p>${data.message}<br>並び順: 閾値オッズが低い(=買いやすい)順</p>`;
    html += `<table><tr><th>券種</th><th>買い目</th><th>推定勝率</th><th>閾値オッズ</th></tr>`;
    for (const r of data.results) {
      html += `<tr><td>${r.bet_type}</td><td>${r.combination}</td><td>${r.estimated_win_prob_pct}%</td><td><strong>${r.threshold_odds}倍以上</strong></td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

async function recordRacePlanAsPurchases() {
  if (!lastRacePlan || !lastRacePlan.items.length) return;
  if (!confirm(`${lastRacePlan.items.length}点をまとめて購入記録します。実際に投票操作をした後に押してください。よろしいですか？`)) return;

  const items = lastRacePlan.items
    .filter((it) => it.stake && it.stake > 0)
    .map((it) => ({
      race_id: lastRacePlan.raceId,
      bet_type: it.bet_type,
      combination: it.combination,
      stake_amount: it.stake,
      odds_at_purchase: it.odds_value,
      // 以前はここが漏れており、購入履歴のwin_prob_at_purchaseが常に空になっていた。
      // このせいで「想定的中率」が算出できず、勝率帯別の自動補正・実績検証も
      // 正しく機能していなかった(のんの報告により発覚)。
      win_prob_at_purchase: it.estimated_win_prob_pct / 100,
      ev_pct_at_purchase: it.ev_pct,
    }));

  try {
    // 以前は1件ずつ/purchases/を呼んでいたため、件数が多いと時間がかかり、画面遷移で
    // 途中のFetchが切れる不具合があった。1回のリクエストでまとめて記録する。
    const res = await fetch(apiUrl("/purchases/bulk"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    alert(`${data.created_count}件を記録しました。`);
    await refreshBankrollDisplay();
  } catch (e) {
    alert("記録に失敗しました: " + e.message);
  }
}

// ---------- ③ 購入記録 ----------
document.getElementById("recordPurchaseBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  const betType = document.getElementById("purchaseBetType").value;
  const combination = document.getElementById("purchaseCombination").value;
  const stake = parseFloat(document.getElementById("purchaseStake").value);
  const resultBox = document.getElementById("purchaseResult");

  if (!raceId || !betType || !combination || !stake) {
    alert("すべての項目を入力してください");
    return;
  }

  try {
    const res = await fetch(apiUrl("/purchases/"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        race_id: parseInt(raceId),
        bet_type: betType,
        combination,
        stake_amount: stake,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    resultBox.textContent = `記録しました(ID:${data.id})。結果が分かったら別途更新してください。`;
    await refreshBankrollDisplay();
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

// ---------- ③-2 結果確定 ----------
document.getElementById("loadPendingBtn").addEventListener("click", async () => {
  const box = document.getElementById("pendingList");
  box.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/pending"));
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    if (!data.length) {
      box.textContent = "未確定の購入はありません。";
      return;
    }

    // レースごとにグループ化
    const byRace = {};
    for (const p of data) {
      if (!byRace[p.race_id]) {
        byRace[p.race_id] = { venue_name: p.venue_name, race_number: p.race_number, items: [] };
      }
      byRace[p.race_id].items.push(p);
    }

    box.innerHTML = "";
    for (const [raceId, group] of Object.entries(byRace)) {
      const div = document.createElement("div");
      div.style.borderBottom = "1px solid #334155";
      div.style.padding = "10px 0";
      let itemsHtml = group.items.map(p => `${p.bet_type} ${p.combination}(${p.stake_amount}円)`).join(" / ");
      div.innerHTML = `
        <p><strong>${group.venue_name} ${group.race_number}R</strong><br>未確定: ${itemsHtml}</p>
        <label>実際の着順(例: 2-5-1 = 1着2番,2着5番,3着1番。同着は"="で区切る 例: 7-14=9)</label>
        <input type="text" placeholder="2-5-1(同着なら 7-14=9)" id="result_${raceId}">
        <button data-race="${raceId}" class="confirmResultBtn">この着順で一括確定する</button>
        <button data-race="${raceId}" class="discardPendingBtn" style="background:#64748b;">実際は投票しなかった(この分を破棄)</button>
        <div id="confirmMsg_${raceId}" class="result-box"></div>
      `;
      box.appendChild(div);
    }

    document.querySelectorAll(".discardPendingBtn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const raceId = btn.dataset.race;
        if (!confirm("このレースの未確定分を破棄します(実際に投票しなかった記録として削除)。よろしいですか？")) return;
        try {
          const res = await fetch(apiUrl(`/purchases/pending/by-race/${raceId}`), { method: "DELETE" });
          const data = await res.json();
          if (!res.ok) throw new Error(JSON.stringify(data));
          document.getElementById("loadPendingBtn").click();
        } catch (e) {
          alert("破棄エラー: " + e.message);
        }
      });
    });

    document.querySelectorAll(".confirmResultBtn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const raceId = btn.dataset.race;
        const actualResult = document.getElementById(`result_${raceId}`).value.trim();
        const msgBox = document.getElementById(`confirmMsg_${raceId}`);
        if (!actualResult) {
          alert("着順を入力してください(例: 2-5-1)");
          return;
        }
        msgBox.textContent = "確定中...";
        try {
          const res = await fetch(
            apiUrl(`/races/${raceId}/confirm-result?actual_result=${encodeURIComponent(actualResult)}`),
            { method: "POST" }
          );
          const result = await res.json();
          if (!res.ok) throw new Error(JSON.stringify(result));
          const winCount = result.updated.filter(u => u.result === "win").length;
          let msg = `${result.updated_count}件を確定しました(的中${winCount}件)。払戻額は最終オッズが未入力の場合、購入時オッズで概算しています。\n`;
          msg += result.updated
            .map(u => `${u.result === "win" ? "🟢的中" : "✗ハズレ"} ${u.bet_type} ${u.combination}${u.result === "win" ? `(払戻${u.payout_amount}円${u.payout_is_estimated ? "・概算" : ""})` : ""}`)
            .join("\n");
          msgBox.textContent = msg;
          await refreshBankrollDisplay();
          await loadRaces(); // 確定済みレースはもう投票できないため一覧(投票タブ)から消す
        } catch (e) {
          msgBox.textContent = "エラー: " + e.message;
        }
      });
    });
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
});

// ---------- ④ 資金管理シミュレーション ----------

// 検証タブを開いた時、実績の的中率・投資額加重平均オッズを自動入力する
// (架空の数字ではなく、のん自身の実際の投票実績を土台にシミュレートするため)
let simDefaultsFilled = false;
async function fillSimDefaultsFromActuals() {
  if (simDefaultsFilled) return; // 一度埋めたら、以降はユーザーの手入力を尊重して上書きしない
  try {
    const res = await fetch(apiUrl("/purchases/stats"));
    const data = await res.json();
    if (data.message) return; // 実績データがまだ無い
    if (data.overall_win_rate_pct !== undefined && data.overall_win_rate_pct !== null) {
      document.getElementById("simWinProb").value = data.overall_win_rate_pct;
    }
    if (data.avg_odds_weighted !== null && data.avg_odds_weighted !== undefined) {
      document.getElementById("simOdds").value = data.avg_odds_weighted;
    }
    simDefaultsFilled = true;
    const note = document.getElementById("simDefaultsNote");
    if (note) note.textContent = `実績値を自動入力しました(的中率${data.overall_win_rate_pct}%・投資額加重平均オッズ${data.avg_odds_weighted}倍、総ベット数${data.total_bets}件)。データが増えたら再読み込みしてください。`;
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("reloadSimDefaultsBtn").addEventListener("click", () => {
  simDefaultsFilled = false;
  fillSimDefaultsFromActuals();
});

document.getElementById("runSimBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("simResult");
  let bankroll;
  try {
    const bankrollRes = await fetch(apiUrl("/bankroll/"));
    const bankrollData = await bankrollRes.json();
    bankroll = bankrollData.current_balance;
  } catch (e) {
    resultBox.textContent = "証拠金の取得に失敗しました。先に証拠金タブで設定してください。";
    return;
  }
  if (!bankroll) {
    resultBox.textContent = "証拠金が未設定です。証拠金タブで初期額を設定してください。";
    return;
  }
  const winProb = parseFloat(document.getElementById("simWinProb").value) / 100;
  const odds = parseFloat(document.getElementById("simOdds").value);
  const racePct = parseFloat(document.getElementById("simRacePct").value) / 100;
  const betsPerRace = parseInt(document.getElementById("simBetsPerRace").value);
  const numRaces = parseInt(document.getElementById("simNumRaces").value);
  // 1レースあたりの投資上限を、そのレースの点数で均等に割った額を1点あたりの賭け比率とする
  // (race_planの1レース上限比率と同じ考え方。のんの要望により、入力をレース単位に変更)
  const stakeFraction = racePct / betsPerRace;
  const numBets = betsPerRace * numRaces;

  resultBox.textContent = "シミュレーション実行中...";
  try {
    const res = await fetch(apiUrl("/simulation/bankruptcy"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initial_bankroll: bankroll,
        win_prob: winProb,
        odds_value: odds,
        stake_fraction: stakeFraction,
        num_bets_per_trial: numBets,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    resultBox.textContent =
      `破産確率(資金が${data.ruin_threshold_pct * 100}%以下になる確率): ${data.ruin_probability_pct}%\n` +
      `黒字化率(初期資金より増えて終わった割合): ${data.profit_probability_pct}%\n` +
      `平均最終資金: ${data.average_final_bankroll}円 / 中央値: ${data.median_final_bankroll}円\n` +
      `(試行回数: ${data.num_trials}回 × ${numRaces}レース分・1レース${betsPerRace}点・1点あたり賭け比率${(stakeFraction * 100).toFixed(3)}%)`;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

// ---------- ⑤ 実績検証 ----------
function renderBucketTable(title, bucketObj) {
  if (!bucketObj || Object.keys(bucketObj).length === 0) return "";
  let html = `<p style="margin-top:10px;"><strong>${title}</strong></p><table><tr><th>区分</th><th>件数</th><th>的中率</th><th>想定的中率</th><th>実績</th><th>想定回収率</th></tr>`;
  for (const [key, v] of Object.entries(bucketObj)) {
    const cls = v.expectancy_pct > 0 ? "ev-positive" : "";
    html += `<tr class="${cls}"><td>${key}</td><td>${v.count}</td><td>${v.win_rate_pct}%</td><td>${v.expected_win_rate_pct ?? "-"}${v.expected_win_rate_pct !== null ? "%" : ""}</td><td>${v.expectancy_pct}%</td><td>${v.expected_roi_pct ?? "-"}${v.expected_roi_pct !== null ? "%" : ""}</td></tr>`;
  }
  html += "</table>";
  return html;
}

document.getElementById("loadStatsBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/stats"));
    const data = await res.json();
    if (data.message) {
      resultBox.textContent = data.message;
      return;
    }
    let html = `<p><strong>実績収支率: ${data.overall_roi_pct}%</strong>(実績損益: ${data.overall_profit_total}円 / 100%が損益分岐点。想定回収率: ${data.expected_roi_pct ?? "-"}${data.expected_roi_pct !== null ? "%" : ""}(想定損益: ${data.expected_profit_total ?? "-"}円)・同じく100%が損益分岐点。総ベット数: ${data.total_bets}件)</p>`;
    html += `<p>的中率: ${data.overall_win_rate_pct}%(想定的中率: ${data.expected_win_rate_pct ?? "-"}${data.expected_win_rate_pct !== null ? "%" : ""}、AIが購入時点で見積もっていた平均勝率)</p>`;
    if (data.calibration_significance) {
      const cs = data.calibration_significance;
      const cls = cs.p_value_pct < 5 ? ' style="color:#ef4444;font-weight:bold;"' : (cs.p_value_pct < 20 ? ' style="color:#f59e0b;"' : "");
      html += `<p${cls}>📊 [買い目単位] このズレが偶然起きる確率: ${cs.p_value_pct}%(${cs.judgement})<br><span class="note">計算に使った件数: ${cs.n_used}件中${cs.wins_used}的中(想定的中率${cs.predicted_prob_used_pct}%)。${cs.n_used !== data.total_bets ? `⚠️総ベット数${data.total_bets}件と一致していません。${cs.note}` : ""}</span></p>`;
      if (cs.race_level && cs.race_level.n_races) {
        html += `<p class="note">📊 [レース単位・参考] ${cs.race_level.n_races}レース中${cs.race_level.profit_races}レースが黒字(${cs.race_level.profit_race_rate_pct}%)。${cs.race_level.note}</p>`;
      }
    }
    html += `<p class="note">${data.note}</p>`;

    if (data.best_conditions_ranking && data.best_conditions_ranking.length) {
      html += `<p style="margin-top:12px;"><strong>🏆 好調な条件(実績が高い順)</strong></p>`;
      html += `<table><tr><th>切り口</th><th>条件</th><th>件数</th><th>的中率</th><th>想定的中率</th><th>実績</th><th>想定回収率</th></tr>`;
      for (const r of data.best_conditions_ranking) {
        html += `<tr class="ev-positive"><td>${r.category}</td><td>${r.condition}</td><td>${r.count}</td><td>${r.win_rate_pct}%</td><td>${r.expected_win_rate_pct ?? "-"}${r.expected_win_rate_pct !== null ? "%" : ""}</td><td>${r.expectancy_pct}%</td><td>${r.expected_roi_pct ?? "-"}${r.expected_roi_pct !== null ? "%" : ""}</td></tr>`;
      }
      html += `</table>`;
    }
    if (data.worst_conditions_ranking && data.worst_conditions_ranking.length) {
      html += `<p style="margin-top:12px;"><strong>⚠️ 不調な条件(見直しの手がかり)</strong></p>`;
      html += `<table><tr><th>切り口</th><th>条件</th><th>件数</th><th>的中率</th><th>想定的中率</th><th>実績</th><th>想定回収率</th></tr>`;
      for (const r of data.worst_conditions_ranking) {
        html += `<tr><td>${r.category}</td><td>${r.condition}</td><td>${r.count}</td><td>${r.win_rate_pct}%</td><td>${r.expected_win_rate_pct ?? "-"}${r.expected_win_rate_pct !== null ? "%" : ""}</td><td>${r.expectancy_pct}%</td><td>${r.expected_roi_pct ?? "-"}${r.expected_roi_pct !== null ? "%" : ""}</td></tr>`;
      }
      html += `</table>`;
    }

    if (data.combo_buckets && Object.values(data.combo_buckets).some(v => Object.keys(v).length > 0)) {
      html += `<p style="margin-top:14px;"><strong>🔍 2軸の組み合わせ検証(単一条件だけでは分からない交絡を確認)</strong></p>`;
      html += `<p class="note">単一条件(例:「グレード別」)だけでは、本当の原因が別の要因(季節など)にある可能性を見分けられません。以下は2つの条件を掛け合わせた集計です。</p>`;
      for (const [comboTitle, comboData] of Object.entries(data.combo_buckets)) {
        html += renderBucketTable(comboTitle, comboData);
      }
    }

    html += `<p style="margin-top:14px;"><strong>詳細(単一条件別の内訳)</strong></p>`;
    html += renderBucketTable("券種別", data.by_bet_type);
    html += renderBucketTable("勝率帯別", data.by_win_prob_bucket);
    html += renderBucketTable("バンク別", data.by_bank);
    html += renderBucketTable("ライン絡み別", data.by_line_match);
    html += renderBucketTable("バンク先行有利度別", data.by_bank_lead_advantage);
    html += renderBucketTable("レースステージ別", data.by_race_stage);
    html += renderBucketTable("季節別", data.by_season);
    html += renderBucketTable("グレード別", data.by_grade);
    html += renderBucketTable("買い目内平均競走得点別", data.by_race_score);
    html += renderBucketTable("買い目内脚質構成別", data.by_leg_style);
    html += renderBucketTable("人気集中度パターン別", data.by_popularity_pattern);
    if (data.odds_drift && !data.odds_drift.message) {
      html += `<p style="margin-top:10px;"><strong>オッズ変動の影響</strong></p>`;
      html += `<p>サンプル数: ${data.odds_drift.sample_count}件 / 平均乖離: ${data.odds_drift.avg_odds_drift_pct}% / 不利方向の割合: ${data.odds_drift.worsened_ratio_pct}%</p>`;
    }
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

document.getElementById("loadCalibrationBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/calibration"));
    const data = await res.json();
    let html = "";
    if (data.overall) {
      const o = data.overall;
      const sign = o.deviation_pct > 0 ? "+" : "";
      html += `<p><strong>全体のズレ: ${sign}${o.deviation_pct}pt</strong>(実績的中率${o.actual_win_rate_pct}% - 予想平均${o.predicted_avg_prob_pct}%、${o.sample_count}件)<br>プラスなら予想が控えめ(実際はもっと当たっている)、マイナスなら予想が強気すぎ(実際はもっと外れている)ことを意味します。</p>`;
      const cls = o.significance_p_value_pct < 5 ? ' style="color:#ef4444;font-weight:bold;"' : (o.significance_p_value_pct < 20 ? ' style="color:#f59e0b;"' : "");
      html += `<p${cls}>📊 このズレが偶然起きる確率: ${o.significance_p_value_pct}%(5%未満=偶然では説明しにくい、20%以上=まだ偶然の範囲内)</p>`;
    } else {
      html += "<p>まだ確定した購入履歴がありません。</p>";
    }
    html += "<p>以下は勝率帯ごとの内訳です。試行数が必要数に達すると、以降の期待値計算に自動で反映されます。</p>";
    html += `<table><tr><th>勝率帯</th><th>試行数</th><th>必要数</th><th>状態</th><th>実績的中率</th><th>予想平均</th><th>ズレ</th><th>偶然の確率</th><th>補正係数</th></tr>`;
    for (const [bucket, info] of Object.entries(data.buckets || {})) {
      const status = info.is_reliable ? '<span class="ev-positive">適用中</span>' : "未達(補正なし)";
      let devText = "-";
      if (info.deviation_pct !== null && info.deviation_pct !== undefined) {
        const sign = info.deviation_pct > 0 ? "+" : "";
        const cls = Math.abs(info.deviation_pct) >= 5 ? ' style="color:#f59e0b;font-weight:bold;"' : "";
        devText = `<span${cls}>${sign}${info.deviation_pct}pt</span>`;
      }
      let pText = "-";
      if (info.significance_p_value_pct !== null && info.significance_p_value_pct !== undefined) {
        const cls = info.significance_p_value_pct < 5 ? ' style="color:#ef4444;font-weight:bold;"' : "";
        pText = `<span${cls}>${info.significance_p_value_pct}%</span>`;
      }
      html += `<tr><td>${bucket}</td><td>${info.sample_count}</td><td>${info.required_sample_count}</td><td>${status}</td><td>${info.actual_win_rate_pct ?? "-"}%</td><td>${info.predicted_avg_prob_pct ?? "-"}%</td><td>${devText}</td><td>${pText}</td><td>${info.calibration_factor}倍</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

document.getElementById("loadSourceWeightsBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/source-weights"));
    const data = await res.json();
    let html = `<p>${data.reason}</p>`;
    html += `<table><tr><th>予想元</th><th>重み</th>${data.based_on_actual_data ? "<th>Brierスコア(低い方が精度高)</th>" : ""}</tr>`;
    html += `<tr><td>tipstar勝率</td><td>${(data.app_weight * 100).toFixed(1)}%</td>${data.based_on_actual_data ? `<td>${data.app_brier_score}</td>` : ""}</tr>`;
    html += `<tr><td>AI推定</td><td>${(data.ai_weight * 100).toFixed(1)}%</td>${data.based_on_actual_data ? `<td>${data.ai_brier_score}</td>` : ""}</tr>`;
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

document.getElementById("loadBigExpectedBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/big-expected-bets"));
    const items = await res.json();
    if (!items.length) {
      resultBox.textContent = "対象データがありません(想定期待値が記録された購入がまだありません)。";
      return;
    }
    let html = `<p class="note">想定利益(投資額×想定期待値)が大きい順です。上位の買い目が的中/不的中どちらだったかで、少数の高期待値な買い目が結果全体をどれだけ左右しているかが分かります。誤って重複登録した等の場合は削除できます。</p>`;
    html += `<table><tr><th>レース</th><th>券種</th><th>買い目</th><th>投票額</th><th>想定勝率</th><th>オッズ</th><th>想定利益</th><th>結果</th><th>実損益</th><th></th></tr>`;
    for (const it of items) {
      const resultLabel = it.result === "pending" ? "未確定" : (it.result === "win" ? "🟢的中" : "✗ハズレ");
      const cls = it.result === "win" ? "ev-positive" : (it.result === "lose" ? "ev-skip" : "");
      html += `<tr class="${cls}"><td>${it.venue_name}${it.race_number}R</td><td>${it.bet_type}</td><td>${it.combination}</td><td>${it.stake_amount}円</td><td>${it.win_prob_at_purchase_pct ?? "-"}%</td><td>${it.odds_at_purchase ?? "-"}</td><td>+${it.expected_profit}円</td><td>${resultLabel}</td><td>${it.actual_profit !== null ? it.actual_profit + "円" : "-"}</td><td><button data-purchase-id="${it.purchase_id}" class="deleteBigExpectedBtn" style="width:auto;padding:4px 8px;font-size:12px;background:#7f1d1d;">削除</button></td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;

    document.querySelectorAll(".deleteBigExpectedBtn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.purchaseId;
        if (!confirm("この購入記録を削除します(確定済みの場合、実績データも書き換わります。証拠金残高は自動調整されません)。よろしいですか？")) return;
        try {
          const res = await fetch(apiUrl(`/purchases/${id}`), { method: "DELETE" });
          const data = await res.json();
          if (!res.ok) throw new Error(JSON.stringify(data));
          document.getElementById("loadBigExpectedBtn").click();
        } catch (e) {
          alert("削除エラー: " + e.message);
        }
      });
    });
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

// ---------- 詳細設定(localStorageに保存・復元) ----------
const SETTINGS_STORAGE_KEY = "keirinEvToolSettings";
const SETTINGS_INPUT_IDS = ["minProbInput", "minEvInput", "maxRacePctInput", "maxItemsInput", "thresholdLimitInput"];

function loadSettingsFromStorage() {
  try {
    const saved = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "{}");
    for (const id of SETTINGS_INPUT_IDS) {
      if (saved[id] !== undefined) {
        const el = document.getElementById(id);
        if (el) el.value = saved[id];
      }
    }
  } catch (e) {
    console.error("設定の読み込みに失敗:", e);
  }
}

function saveSettingsToStorage() {
  const values = {};
  for (const id of SETTINGS_INPUT_IDS) {
    const el = document.getElementById(id);
    if (el) values[id] = el.value;
  }
  localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(values));
}

SETTINGS_INPUT_IDS.forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", saveSettingsToStorage);
});
loadSettingsFromStorage();

// 初回ロード
loadRaces();
refreshBankrollDisplay();
