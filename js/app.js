// バックエンドのURLは固定(このプロジェクト専用のRenderデプロイ先)
const API_BASE_URL = "https://keirin-ev-tool.onrender.com";

function apiUrl(path) {
  return API_BASE_URL + path;
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
    resultBox.textContent = `解析完了: ${data.processed_files}枚処理\n` +
      data.results.map(r => `- ${r.filename}: ${r.screen_type} (レースID:${r.race_id} 選手${r.entries_found}件 オッズ${r.odds_found}件)`).join("\n");
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

    html += `<table><tr><th>車番</th><th>選手名</th><th>地区</th><th>地元</th><th>脚質</th><th>アプリ勝率</th><th>AI推定</th><th>合成勝率</th></tr>`;
    for (const e of data.entries) {
      const status = e.ready_for_ev ? "" : ' style="color:#ef4444;"';
      const localMark = e.is_local === true ? "🏠地元" : (e.is_local === false ? "-" : "不明");
      html += `<tr${status}><td>${e.car_number}</td><td>${e.player_name}</td><td>${e.region ?? "-"}</td><td>${localMark}</td><td>${e.leg_style ?? "-"}</td><td>${e.app_win_rate ?? "-"}</td><td>${e.ai_win_prob ?? "-"}</td><td>${e.blended_win_prob ?? "未取得"}</td></tr>`;
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
  const kellyCoef = parseFloat(document.getElementById("kellyCoefInput").value);
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
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    let html = `<table><tr><th>券種</th><th>買い目</th><th>推定勝率</th><th>オッズ</th><th>期待値%</th><th>推奨額</th><th>判定</th><th>自己影響</th></tr>`;
    for (const r of data.results) {
      const cls = r.is_skip ? "ev-skip" : (r.is_recommended ? "ev-positive" : "");
      const judge = r.is_skip ? "見送り" : (r.is_recommended ? "🟢買い" : "△");
      const impact = r.self_impact_pct === null ? "不明" : `${r.self_impact_pct}%${r.self_impact_warning ? "⚠️" : ""}`;
      html += `<tr class="${cls}"><td>${r.bet_type}</td><td>${r.combination}</td><td>${r.estimated_win_prob_pct}%</td><td>${r.odds_value}</td><td>${r.ev_pct}%</td><td>${r.is_skip ? "-" : r.recommended_stake + "円"}</td><td>${judge}</td><td>${impact}</td></tr>`;
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
  const kellyCoef = parseFloat(document.getElementById("kellyCoefInput").value);
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
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    if (!data.items || data.items.length === 0) {
      resultBox.textContent = data.message || "買い示唆がありませんでした(見送り推奨)";
      return;
    }

    let html = `<p><strong>合計投票額: ${data.total_stake}円</strong>(上限${data.race_budget_cap}円${data.was_scaled_down ? "・上限に合わせて按分済み" : ""})</p>`;
    html += `<p>レース全体の期待値: ${data.race_ev_pct}%(期待利益 約${data.total_expected_profit}円)</p>`;
    html += `<table><tr><th>券種</th><th>買い目</th><th>勝率</th><th>オッズ</th><th>期待値%</th><th>投票額</th><th>自己影響</th></tr>`;
    for (const it of data.items) {
      const impact = it.self_impact_pct === null ? "不明" : `${it.self_impact_pct}%${it.self_impact_warning ? "⚠️" : ""}`;
      html += `<tr class="ev-positive"><td>${it.bet_type}</td><td>${it.combination}</td><td>${it.estimated_win_prob_pct}%</td><td>${it.odds_value}</td><td>${it.ev_pct}%</td><td>${it.stake}円</td><td>${impact}</td></tr>`;
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
    const res = await fetch(apiUrl(`/ev/threshold-table/${raceId}?min_ev_pct=${minEvPct}&min_win_prob=${minProb}&limit=${limit}`), {
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

    box.innerHTML = "";
    for (const p of data) {
      const row = document.createElement("div");
      row.style.borderBottom = "1px solid #334155";
      row.style.padding = "8px 0";
      row.innerHTML = `
        <p>ID:${p.id} ${p.bet_type} ${p.combination} / 購入額${p.stake_amount}円 / 購入時オッズ${p.odds_at_purchase ?? "-"}</p>
        <input type="number" placeholder="最終オッズ(任意)" id="finalOdds_${p.id}" style="width:48%;display:inline-block;">
        <input type="number" placeholder="払戻額(円、外れなら0)" id="payout_${p.id}" style="width:48%;display:inline-block;">
        <button data-id="${p.id}" data-result="win" class="resultBtn" style="background:#22c55e;width:48%;display:inline-block;">的中</button>
        <button data-id="${p.id}" data-result="lose" class="resultBtn" style="background:#ef4444;width:48%;display:inline-block;">不的中</button>
      `;
      box.appendChild(row);
    }

    document.querySelectorAll(".resultBtn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        const result = btn.dataset.result;
        const payoutInput = document.getElementById(`payout_${id}`).value;
        const finalOddsInput = document.getElementById(`finalOdds_${id}`).value;
        const payout = result === "win" ? (parseFloat(payoutInput) || 0) : 0;
        const finalOdds = finalOddsInput === "" ? null : parseFloat(finalOddsInput);

        try {
          const res = await fetch(apiUrl(`/purchases/${id}/result`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ result, payout_amount: payout, final_odds: finalOdds }),
          });
          if (!res.ok) throw new Error(await res.text());
          alert("記録しました");
          await refreshBankrollDisplay();
          document.getElementById("loadPendingBtn").click();
        } catch (e) {
          alert("エラー: " + e.message);
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
document.getElementById("loadStatsBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/stats"));
    const data = await res.json();
    resultBox.textContent = JSON.stringify(data, null, 2);
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

// 初回ロード
loadRaces();
refreshBankrollDisplay();
