// APIベースURLはlocalStorageに保存(ブラウザ内のみ、サーバー送信なし)
function getApiBase() {
  return localStorage.getItem("apiBaseUrl") || "";
}

document.getElementById("apiBaseUrl").value = getApiBase();

document.getElementById("saveConfigBtn").addEventListener("click", () => {
  const url = document.getElementById("apiBaseUrl").value.trim().replace(/\/$/, "");
  localStorage.setItem("apiBaseUrl", url);
  alert("保存しました: " + url);
});

function apiUrl(path) {
  return getApiBase() + path;
}

// ---------- ① スクショ解析 ----------
document.getElementById("uploadBtn").addEventListener("click", async () => {
  const input = document.getElementById("screenshotInput");
  const resultBox = document.getElementById("uploadResult");
  if (!input.files.length) {
    alert("画像を選択してください");
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

document.getElementById("calcEvBtn").addEventListener("click", async () => {
  const raceId = document.getElementById("raceSelect").value;
  const resultBox = document.getElementById("evResult");
  if (!raceId) {
    alert("レースを選択してください");
    return;
  }
  const bankroll = parseFloat(document.getElementById("bankrollInput").value);
  const kellyCoef = parseFloat(document.getElementById("kellyCoefInput").value);
  const minProb = parseFloat(document.getElementById("minProbInput").value) / 100;

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
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));

    let html = `<table><tr><th>券種</th><th>買い目</th><th>推定勝率</th><th>オッズ</th><th>期待値%</th><th>推奨額</th></tr>`;
    for (const r of data.results) {
      const cls = r.is_skip ? "ev-skip" : (r.ev_pct > 0 ? "ev-positive" : "");
      html += `<tr class="${cls}"><td>${r.bet_type}</td><td>${r.combination}</td><td>${r.estimated_win_prob_pct}%</td><td>${r.odds_value}</td><td>${r.ev_pct}%</td><td>${r.is_skip ? "見送り" : r.recommended_stake + "円"}</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

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
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
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

// 初回ロード
loadRaces();
