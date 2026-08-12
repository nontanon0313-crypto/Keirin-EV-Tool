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

  resultBox.textContent = "解析中...(画像枚数によっては数十秒かかります)";

  const formData = new FormData();
  for (const file of input.files) {
    formData.append("files", file);
  }

  try {
    const res = await fetch(apiUrl("/analyze/screenshots"), {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    resultBox.textContent = `解析完了: ${data.processed_files}枚処理${data.error_count ? `(うち${data.error_count}枚は解析失敗)` : ""}\n` +
      data.results.map(r => r.error ? `- ${r.filename}: ❌解析失敗(${r.error})` : `- ${r.filename}: ${r.screen_type} (レースID:${r.race_id} 選手${r.entries_found}件 オッズ${r.odds_found}件)`).join("\n");
    await loadRaces();
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

// ---------- ② レース選択・期待値計算 ----------
async function loadRaces() {
  const select = document.getElementById("raceSelect");
  try {
    const res = await fetch(apiUrl("/races/"));
    const races = await res.json();
    select.innerHTML = races.map(r =>
      `<option value="${r.id}">${r.venue_name} ${r.race_number}R (選手${r.entry_count}/オッズ${r.odds_count})</option>`
    ).join("");
  } catch (e) {
    console.error(e);
  }
}
document.getElementById("loadRacesBtn").addEventListener("click", loadRaces);

document.getElementById("deleteRaceBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  if (!raceId) {
    alert("削除するレースを選択してください");
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

document.getElementById("checkRaceBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  const box = document.getElementById("raceDetailResult");
  if (!raceId) {
    alert("レースを選択してください");
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
      : `<span style="color:#ef4444;">まだ不足しているデータがあります</span></p>`;

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
      html += `<tr${status}><td>${e.car_number}</td><td>${e.player_name}</td><td>${e.region ?? "-"}</td><td>${e.leg_style ?? "-"}</td><td>${e.app_win_rate ?? "-"}</td><td>${e.ai_win_prob ?? "-"}</td><td>${e.blended_win_prob ?? "未取得"}</td></tr>`;
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
});

function getBankrollOverride() {
  const raw = document.getElementById("bankrollInput").value;
  if (raw === "" || raw === null) return null;
  const v = parseFloat(raw);
  return isNaN(v) ? null : v;
}

document.getElementById("calcEvBtn").addEventListener("click", async () => {
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

  resultBox.textContent = "計算中...";
  try {
    const res = await fetch(apiUrl(`/ev/calculate/${raceId}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        race_id: parseInt(raceId),
        bankroll,
        fractional_coefficient: kellyCoef,
        min_win_prob: minProb,
        min_ev_pct: minEvPct,
        rebate_pct: getRebatePct(),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    let html = `<p class="note">期待値マイナス・見送り対象(${data.hidden_negative_count}件)は非表示にしています。回収率100%が収支トントンの基準です。</p>`;
    html += `<table><tr><th>券種</th><th>買い目</th><th>推定勝率</th><th>オッズ</th><th>回収率%</th><th>推奨額</th><th>判定</th><th>自己影響</th></tr>`;
    for (const r of data.results) {
      const cls = r.is_skip ? "ev-skip" : (r.is_recommended ? "ev-positive" : "");
      const judge = r.is_skip ? "見送り" : (r.is_recommended ? "🟢買い" : "△");
      const impact = r.self_impact_pct === null ? "不明" : `${r.self_impact_pct}%${r.self_impact_warning ? "⚠️" : ""}`;
      html += `<tr class="${cls}"><td>${r.bet_type}</td><td>${r.combination}</td><td>${r.estimated_win_prob_pct}%</td><td>${r.odds_value}</td><td>${r.roi_pct}%</td><td>${r.is_skip ? "-" : r.recommended_stake + "円"}</td><td>${judge}</td><td>${impact}</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

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
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    if (!data.items || data.items.length === 0) {
      resultBox.textContent = data.message || "買い示唆がありませんでした(見送り推奨)";
      return;
    }

    let html = `<p><strong>合計投票額: ${data.total_stake}円</strong>(上限${data.race_budget_cap}円)${data.excluded_by_budget_count > 0 ? `<br>予算の都合で${data.excluded_by_budget_count}件は見送りました(期待値が低い順に除外)` : ""}</p>`;
    html += `<p>レース全体の回収率: ${data.race_roi_pct}%(期待利益 約${data.total_expected_profit}円) / レース全体の的中率: 約${data.race_hit_prob_pct}%</p>`;
    html += `<table><tr><th>券種</th><th>買い目</th><th>勝率</th><th>オッズ</th><th>回収率%</th><th>投票額</th><th>自己影響</th></tr>`;
    for (const it of data.items) {
      const impact = it.self_impact_pct === null ? "不明" : `${it.self_impact_pct}%${it.self_impact_warning ? "⚠️" : ""}`;
      html += `<tr class="ev-positive"><td>${it.bet_type}</td><td>${it.combination}</td><td>${it.estimated_win_prob_pct}%</td><td>${it.odds_value}</td><td>${it.roi_pct}%</td><td>${it.stake}円</td><td>${impact}</td></tr>`;
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

  let successCount = 0;
  const errors = [];
  for (const it of lastRacePlan.items) {
    if (!it.stake || it.stake <= 0) continue;
    try {
      const res = await fetch(apiUrl("/purchases/"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          race_id: lastRacePlan.raceId,
          bet_type: it.bet_type,
          combination: it.combination,
          stake_amount: it.stake,
          odds_at_purchase: it.odds_value,
          ev_pct_at_purchase: it.ev_pct,
        }),
      });
      if (res.ok) successCount++;
      else errors.push(it.combination);
    } catch (e) {
      errors.push(it.combination);
    }
  }
  alert(`${successCount}件を記録しました。${errors.length ? "失敗: " + errors.join(", ") : ""}`);
  await refreshBankrollDisplay();
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
        <label>実際の着順(例: 2-5-1 = 1着2番,2着5番,3着1番)</label>
        <input type="text" placeholder="2-5-1" id="result_${raceId}">
        <button data-race="${raceId}" class="confirmResultBtn">この着順で一括確定する</button>
        <div id="confirmMsg_${raceId}" class="result-box"></div>
      `;
      box.appendChild(div);
    }

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
          msgBox.textContent = `${result.updated_count}件を確定しました(的中${winCount}件)。払戻額は最終オッズが未入力の場合、購入時オッズで概算しています。`;
          await refreshBankrollDisplay();
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
document.getElementById("runSimBtn").addEventListener("click", async () => {
  const bankroll = parseFloat(document.getElementById("bankrollInput").value);
  const winProb = parseFloat(document.getElementById("simWinProb").value) / 100;
  const odds = parseFloat(document.getElementById("simOdds").value);
  const stakeFraction = parseFloat(document.getElementById("simStakePct").value) / 100;
  const numBets = parseInt(document.getElementById("simNumBets").value);
  const resultBox = document.getElementById("simResult");

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
      `平均最終資金: ${data.average_final_bankroll}円\n` +
      `(試行回数: ${data.num_trials}回 × ${data.num_bets_per_trial}ベット)`;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

// ---------- ⑤ 実績検証 ----------
function renderBucketTable(title, bucketObj) {
  if (!bucketObj || Object.keys(bucketObj).length === 0) return "";
  let html = `<p style="margin-top:10px;"><strong>${title}</strong></p><table><tr><th>区分</th><th>件数</th><th>的中率</th><th>期待値実績</th></tr>`;
  for (const [key, v] of Object.entries(bucketObj)) {
    const cls = v.expectancy_pct > 0 ? "ev-positive" : "";
    html += `<tr class="${cls}"><td>${key}</td><td>${v.count}</td><td>${v.win_rate_pct}%</td><td>${v.expectancy_pct}%</td></tr>`;
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
    let html = `<p><strong>実績収支率: ${data.overall_roi_pct}%</strong>(100%が損益分岐点。総ベット数: ${data.total_bets}件)</p>`;
    html += `<p class="note">${data.note}</p>`;

    if (data.best_conditions_ranking && data.best_conditions_ranking.length) {
      html += `<p style="margin-top:12px;"><strong>🏆 好調な条件(期待値実績が高い順)</strong></p>`;
      html += `<table><tr><th>切り口</th><th>条件</th><th>件数</th><th>的中率</th><th>期待値実績</th></tr>`;
      for (const r of data.best_conditions_ranking) {
        html += `<tr class="ev-positive"><td>${r.category}</td><td>${r.condition}</td><td>${r.count}</td><td>${r.win_rate_pct}%</td><td>${r.expectancy_pct}%</td></tr>`;
      }
      html += `</table>`;
    }
    if (data.worst_conditions_ranking && data.worst_conditions_ranking.length) {
      html += `<p style="margin-top:12px;"><strong>⚠️ 不調な条件(見直しの手がかり)</strong></p>`;
      html += `<table><tr><th>切り口</th><th>条件</th><th>件数</th><th>的中率</th><th>期待値実績</th></tr>`;
      for (const r of data.worst_conditions_ranking) {
        html += `<tr><td>${r.category}</td><td>${r.condition}</td><td>${r.count}</td><td>${r.win_rate_pct}%</td><td>${r.expectancy_pct}%</td></tr>`;
      }
      html += `</table>`;
    }

    html += `<p style="margin-top:14px;"><strong>詳細(切り口別の全内訳)</strong></p>`;
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
    let html = "<p>各勝率帯の自動補正の状態です。試行数が必要数に達すると、以降の期待値計算に自動で反映されます。</p>";
    html += `<table><tr><th>勝率帯</th><th>試行数</th><th>必要数</th><th>状態</th><th>実績的中率</th><th>AI推定平均</th><th>補正係数</th></tr>`;
    for (const [bucket, info] of Object.entries(data)) {
      const status = info.is_reliable ? '<span class="ev-positive">適用中</span>' : "未達(補正なし)";
      html += `<tr><td>${bucket}</td><td>${info.sample_count}</td><td>${info.required_sample_count}</td><td>${status}</td><td>${info.actual_win_rate_pct ?? "-"}%</td><td>${info.predicted_avg_prob_pct ?? "-"}%</td><td>${info.calibration_factor}倍</td></tr>`;
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

// 初回ロード
loadRaces();
refreshBankrollDisplay();
