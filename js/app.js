// バックエンドのURLは固定(このプロジェクト専用のRenderデプロイ先)
const API_BASE_URL = "https://keirin-ev-tool.onrender.com";

function apiUrl(path) {
  return API_BASE_URL + path;
}

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

    html += `<table><tr><th>車番</th><th>選手名</th><th>脚質</th><th>アプリ勝率</th><th>AI推定</th><th>合成勝率</th></tr>`;
    for (const e of data.entries) {
      const status = e.ready_for_ev ? "" : ' style="color:#ef4444;"';
      html += `<tr${status}><td>${e.car_number}</td><td>${e.player_name}</td><td>${e.leg_style ?? "-"}</td><td>${e.app_win_rate ?? "-"}</td><td>${e.ai_win_prob ?? "-"}</td><td>${e.blended_win_prob ?? "未取得"}</td></tr>`;
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

    let html = `<table><tr><th>券種</th><th>買い目</th><th>推定勝率</th><th>オッズ</th><th>期待値%</th><th>推奨額</th><th>判定</th></tr>`;
    for (const r of data.results) {
      const cls = r.is_skip ? "ev-skip" : (r.is_recommended ? "ev-positive" : "");
      const judge = r.is_skip ? "見送り" : (r.is_recommended ? "🟢買い" : "△");
      html += `<tr class="${cls}"><td>${r.bet_type}</td><td>${r.combination}</td><td>${r.estimated_win_prob_pct}%</td><td>${r.odds_value}</td><td>${r.ev_pct}%</td><td>${r.is_skip ? "-" : r.recommended_stake + "円"}</td><td>${judge}</td></tr>`;
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
  const bankroll = parseFloat(document.getElementById("bankrollInput").value);
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
    html += `<table><tr><th>券種</th><th>買い目</th><th>勝率</th><th>オッズ</th><th>期待値%</th><th>投票額</th></tr>`;
    for (const it of data.items) {
      html += `<tr class="ev-positive"><td>${it.bet_type}</td><td>${it.combination}</td><td>${it.estimated_win_prob_pct}%</td><td>${it.odds_value}</td><td>${it.ev_pct}%</td><td>${it.stake}円</td></tr>`;
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
