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
let raceListScope = "today"; // "today" | "upcoming"

async function loadRaces(selectRaceId) {
  const select = document.getElementById("raceSelect");
  const previousValue = select.value;
  try {
    let races;
    if (raceListScope === "today") {
      const res = await fetch(apiUrl("/races/today"));
      const data = await res.json();
      races = data.map(r => ({
        id: r.race_id, race_date: null, venue_name: r.venue_name, race_number: r.race_number,
        entry_count: r.riders_count, odds_count: null,
        label_extra: `${r.post_time ? r.post_time + " " : ""}${r.predicted ? "予想済み" : "未予想"}${r.actual_result ? " ・結果確定済み" : ""}`,
      }));
    } else if (raceListScope === "upcoming") {
      const res = await fetch(apiUrl("/races/upcoming?within_min=30"));
      const data = await res.json();
      races = data.map(r => ({
        id: r.race_id, race_date: null, venue_name: r.venue_name, race_number: r.race_number,
        entry_count: r.riders_count, odds_count: null,
        label_extra: `あと${r.mins_to_post}分(${r.post_time}) ${r.predicted ? "予想済み" : "未予想"}`,
      }));
    } else {
      const res = await fetch(apiUrl("/races/"));
      const data = await res.json();
      races = data.map(r => ({ ...r, label_extra: null }));
    }
    select.innerHTML = races.map(r =>
      `<option value="${r.id}">${r.race_date ? r.race_date + " " : ""}${r.venue_name} ${r.race_number}R${r.label_extra ? " " + r.label_extra : ` (選手${r.entry_count}/オッズ${r.odds_count})`}</option>`
    ).join("");
    if (!races.length) {
      select.innerHTML = `<option value="">(該当レースなし)</option>`;
    }
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

function setRaceFilterButtons(active) {
  const map = { today: "raceFilterTodayBtn", upcoming: "raceFilterUpcomingBtn" };
  for (const [key, id] of Object.entries(map)) {
    document.getElementById(id).style.background = key === active ? "" : "#475569";
  }
}
document.getElementById("raceFilterTodayBtn").addEventListener("click", () => {
  raceListScope = "today"; setRaceFilterButtons("today"); loadRaces();
});
document.getElementById("raceFilterUpcomingBtn").addEventListener("click", () => {
  raceListScope = "upcoming"; setRaceFilterButtons("upcoming"); loadRaces();
});

document.getElementById("loadFavoritesBtn").addEventListener("click", async () => {
  const box = document.getElementById("favoritesResult");
  box.textContent = "読み込み中...";
  const minProb = (parseFloat(document.getElementById("favoritesMinProb").value) || 25) / 100;
  try {
    const res = await fetch(apiUrl(`/races/favorites?min_win_prob=${minProb}`));
    clearTimeout(timer);
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    if (!data.length) {
      box.textContent = "該当する本命候補はありません。";
      return;
    }
    let html = `<table><tr><th>会場</th><th>発走</th><th>車番</th><th>選手名</th><th>勝率</th></tr>`;
    for (const f of data) {
      html += `<tr><td>${f.venue_name}${f.race_number}R</td><td>${f.post_time || "-"}</td><td>${f.car_number}</td><td>${f.player_name}</td><td>${f.win_prob_pct}%</td></tr>`;
    }
    html += "</table>";
    box.innerHTML = html;
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
});


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
    alert("全ての記録を削除しました(過去レース含む)");
    document.getElementById("raceSelect").innerHTML = "";
    const pending = document.getElementById("pendingList");
    if (pending) pending.textContent = "";
    const fav = document.getElementById("favoritesResult");
    if (fav) fav.textContent = "";
    const detail = document.getElementById("raceDetailResult");
    if (detail) detail.textContent = "";
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

    html += `<table><tr><th>車番</th><th>選手名</th><th>地区</th><th>脚質</th><th>競走得点</th><th>S</th><th>H</th><th>B</th><th>決まり手(逃/捲/差/マ)</th><th>直近着順</th><th>コメント</th><th>アプリ勝率</th><th>AI推定</th><th>合成勝率</th></tr>`;
    for (const e of data.entries) {
      const status = e.ready_for_ev ? "" : ' style="color:#ef4444;"';
      const appWinRateLabel = `${e.app_win_rate ?? "-"}${e.zero_app_win_rate_warning ? " ⚠️欠場でないか要確認" : ""}`;
      const kimarite = `${e.kimarite_nige ?? "-"}/${e.kimarite_makuri ?? "-"}/${e.kimarite_sashi ?? "-"}/${e.kimarite_mark ?? "-"}`;
      const finishes = `${e.finish_1st ?? "-"}/${e.finish_2nd ?? "-"}/${e.finish_3rd ?? "-"}`;
      html += `<tr${status}><td>${e.car_number}</td><td>${e.player_name}</td><td>${e.region ?? "-"}${e.is_local ? "(地元)" : ""}</td><td>${e.leg_style ?? "-"}</td><td>${e.race_score ?? "-"}</td><td>${e.s_count ?? "-"}</td><td>${e.h_count ?? "-"}</td><td>${e.b_count ?? "-"}</td><td>${kimarite}</td><td>${finishes}</td><td>${e.pre_race_comment ?? "-"}</td><td>${appWinRateLabel}</td><td>${e.ai_win_prob ?? "-"}</td><td>${e.blended_win_prob ?? "未取得"}</td></tr>`;
    }
    html += `</table>`;
    html += `<p class="note">得点=競走得点、S/H/B=各回数、決まり手は逃げ/捲り/差し/マークの回数、直近着順は1着/2着/3着の回数です。数字が全て「-」の場合、その項目がOCRで読み取れていません。</p>`;


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
        max_race_pct: (function() {
          const useSim = document.getElementById("useSimRaceCapCheckbox") && document.getElementById("useSimRaceCapCheckbox").checked;
          if (useSim) {
            const v = parseFloat(document.getElementById("simRacePct") && document.getElementById("simRacePct").value);
            if (!Number.isNaN(v) && v > 0) return v / 100;
          }
          return parseFloat(document.getElementById("maxRacePctInput").value) / 100;
        })(),
        rebate_pct: getRebatePct(),
        max_items: parseInt(document.getElementById("maxItemsInput").value) || 20,
        apply_calibration: isCalibrationApplyEnabled(),
        apply_performance_gates: document.getElementById("applyPerformanceGatesCheckbox")
          ? document.getElementById("applyPerformanceGatesCheckbox").checked : true,
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
    html += `<table><tr><th>券種</th><th>買い目</th><th>勝率</th><th>オッズ</th><th>回収率%</th><th>投票額</th><th>予想精度</th><th>充足度</th></tr>`;
    for (const it of data.items) {
      const probLabel = `${it.estimated_win_prob_pct}%${it.low_prob_warning ? " ⚠️低確率帯(未補正)" : ""}`;
      const acc = it.prediction_accuracy_pct;
      const accLabel = (acc === null || acc === undefined)
        ? `<span class="note">データ無</span>`
        : (() => {
            const accColor = acc >= 90 ? "#22c55e" : (acc <= 70 ? "#ef4444" : "#f59e0b");
            return `<span style="color:${accColor};font-weight:bold;">${acc}%</span>`;
          })();
      const suf = it.data_sufficiency_pct;
      const sufColor = suf >= 90 ? "#22c55e" : (suf <= 10 ? "#ef4444" : "#f59e0b");
      const sufLabel = `<span style="color:${sufColor};">${suf}%</span>`;
      html += `<tr class="ev-positive"><td>${it.bet_type}</td><td>${it.combination}</td><td>${probLabel}</td><td>${it.odds_value}</td><td>${it.roi_pct}%</td><td>${it.stake}円</td><td>${accLabel}</td><td>${sufLabel}</td></tr>`;
    }
    html += "</table>";
    html += `<p class="note">予想精度は、その勝率帯の予想確率と実績的中率がどれだけ一致しているか(🟢90%以上=よく一致、🟡中間、🔴70%以下=ズレ大)を示す指標です。充足度は、その一致度がどれだけ実績データに裏付けられているか(🟢90%以上=十分な実績あり、🟡中間、🔴10%以下=ほぼ未検証)を示します。データ無=まだその勝率帯の実績が0件。どちらも表示専用で、確率計算や投票内容には影響しません。</p>`;
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
      win_prob_at_purchase: it.win_prob != null ? it.win_prob : it.estimated_win_prob_pct / 100,
      win_prob_raw: it.win_prob_raw != null ? it.win_prob_raw : null,
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
    const [pendingRes, awaitingRes] = await Promise.all([
      fetch(apiUrl("/purchases/pending")),
      fetch(apiUrl("/purchases/races-awaiting-result")),
    ]);
    const data = await pendingRes.json();
    if (!pendingRes.ok) throw new Error(JSON.stringify(data));
    const awaitingRaces = await awaitingRes.json();
    if (!awaitingRes.ok) throw new Error(JSON.stringify(awaitingRaces));

    // レースごとにグループ化(購入がある分)
    const byRace = {};
    for (const p of data) {
      if (!byRace[p.race_id]) {
        byRace[p.race_id] = { venue_name: p.venue_name, race_number: p.race_number, items: [] };
      }
      byRace[p.race_id].items.push(p);
    }
    // 購入0件だったレースも一覧に加える(買い示唆なし・見送りだったレースの結果記録用。
    // 以前はここに出てこず、結果を記録する手段が無かった。のんの指摘により追加)
    for (const r of awaitingRaces) {
      if (!byRace[r.race_id]) {
        byRace[r.race_id] = { venue_name: r.venue_name, race_number: r.race_number, items: [] };
      }
    }

    if (!Object.keys(byRace).length) {
      box.textContent = "結果未記録のレースはありません。";
      return;
    }

    box.innerHTML = "";
    for (const [raceId, group] of Object.entries(byRace)) {
      const div = document.createElement("div");
      div.style.borderBottom = "1px solid #334155";
      div.style.padding = "10px 0";
      const itemsHtml = group.items.length
        ? group.items.map(p => `${p.bet_type} ${p.combination}(${p.stake_amount}円)`).join(" / ")
        : "(このレースは買い示唆なし・購入なしでした)";
      div.innerHTML = `
        <p><strong>${group.venue_name} ${group.race_number}R</strong><br>未確定: ${itemsHtml}</p>
        <label>実際の着順(例: 2-5-1 = 1着2番,2着5番,3着1番。同着は"="で区切る 例: 7-14=9)</label>
        <input type="text" placeholder="2-5-1(同着なら 7-14=9)" id="result_${raceId}">
        <button data-race="${raceId}" class="confirmResultBtn">この着順で一括確定する</button>
        ${group.items.length ? `<button data-race="${raceId}" class="discardPendingBtn" style="background:#64748b;">実際は投票しなかった(この分を破棄)</button>` : ""}
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

// ---------- 🔁 予想を再実行(スクレイピングし直さない) ----------
document.getElementById("reanalyzeAllBtn").addEventListener("click", async () => {
  const box = document.getElementById("reanalyzeProgress");
  const btn = document.getElementById("reanalyzeAllBtn");
  const log = (msg) => { box.textContent += msg + "\n"; box.scrollTop = box.scrollHeight; };

  if (!confirm("全レースの予想・購入記録・EV結果をリセットして再実行します。投票プランの計算は固定100万円で行われ、実際の証拠金残高は変動しません。よろしいですか？")) return;

  btn.disabled = true;
  box.textContent = "";
  try {
    log("レース一覧を取得中...");
    const listRes = await fetch(apiUrl("/races/for-reanalysis"));
    const races = await listRes.json();
    if (!listRes.ok) throw new Error(JSON.stringify(races));
    log(`対象レース: ${races.length}件\n`);

    let doneCount = 0, errorCount = 0;
    for (const race of races) {
      const label = `${race.venue_name}${race.race_number}R(id=${race.id})`;
      log(`--- ${label} ---`);
      try {
        log("  0. リセット中...");
        const resetRes = await fetch(apiUrl(`/races/${race.id}/reset-for-reanalysis`), { method: "POST" });
        const resetData = await resetRes.json();
        if (!resetRes.ok) throw new Error(JSON.stringify(resetData));

        log("  1. 予想(Gemini)中...");
        const estRes = await fetch(apiUrl(`/analyze/estimate/${race.id}`), { method: "POST" });
        const estData = await estRes.json();
        if (!estRes.ok) throw new Error(JSON.stringify(estData));

        log("  2. 投票プラン作成中...");
        const planRes = await fetch(apiUrl(`/ev/race-plan/${race.id}`), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            race_id: race.id,
            bankroll: 1000000, // 検証・集計目的の固定額
            max_race_pct: 1.0, // 1レース上限なし(検証用)
            apply_performance_gates: false, // 検証集計はゲートなし
          }),
        });
        const plan = await planRes.json();
        if (!planRes.ok) throw new Error(JSON.stringify(plan));
        if (plan.skipped_no_odds) {
          log("  → オッズデータが無いためスキップ\n");
          doneCount++;
          continue;
        }
        const items = plan.items || [];
        log(`     買い示唆 ${items.length}件`);

        if (items.length) {
          log("  3. 投票記録中...");
          const bulkBody = {
            items: items.map((it) => ({
              race_id: race.id,
              bet_type: it.bet_type,
              combination: it.combination,
              stake_amount: it.stake,
              odds_at_purchase: it.odds_value,
              win_prob_at_purchase: it.win_prob !== undefined ? it.win_prob : (it.estimated_win_prob_pct || 0) / 100,
              ev_pct_at_purchase: it.ev_pct,
            })),
          };
          const bulkRes = await fetch(apiUrl("/purchases/bulk"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(bulkBody),
          });
          if (!bulkRes.ok) throw new Error(JSON.stringify(await bulkRes.json()));
        }

        if (race.actual_result) {
          log("  4. 結果確定中...");
          const confRes = await fetch(
            apiUrl(`/races/${race.id}/confirm-result?actual_result=${encodeURIComponent(race.actual_result)}`),
            { method: "POST" }
          );
          if (!confRes.ok) throw new Error(JSON.stringify(await confRes.json()));
        } else {
          log("  4. 結果未確定のためスキップ");
        }

        log("  → 完了\n");
        doneCount++;
      } catch (e) {
        log(`  → エラー: ${e.message}\n`);
        errorCount++;
      }
    }
    log(`=== 完了: ${doneCount}件成功 / ${errorCount}件エラー ===`);

    log("\n--- 更新後の集計(実績検証) ---");
    try {
      const stats = await (await fetch(apiUrl("/purchases/stats"))).json();
      if (stats.message) {
        log(`集計を表示: ${stats.message}`);
      } else {
        log(`集計を表示: 実績収支率${stats.overall_roi_pct}%(想定${stats.expected_roi_pct ?? "-"}%) / 的中率${stats.overall_win_rate_pct}%(想定${stats.expected_win_rate_pct ?? "-"}%) / 総ベット${stats.total_bets}件`);
        if (stats.calibration_significance) {
          const cs = stats.calibration_significance;
          log(`  統計的有意性: p値${cs.p_value_pct}% / ${cs.judgement || ""}`);
        }
      }
    } catch (e) { log(`集計を表示: エラー(${e.message})`); }

    try {
      const cal = await (await fetch(apiUrl("/purchases/calibration"))).json();
      if (cal.overall) {
        const o = cal.overall;
        log(`自動補正の状態: 実績的中率${o.actual_win_rate_pct}% vs 予想${o.predicted_avg_prob_pct}%(乖離${o.deviation_pct > 0 ? "+" : ""}${o.deviation_pct}pt, サンプル${o.sample_count}件)`);
      } else {
        log("自動補正の状態: データ不足");
      }
    } catch (e) { log(`自動補正の状態: エラー(${e.message})`); }

    try {
      const cp = await (await fetch(apiUrl("/purchases/car-pick-accuracy"))).json();
      if (cp.message) {
        log(`核となる車番予想の精度: ${cp.message}`);
      } else {
        log(`核となる車番予想の精度: n_races=${cp.n_races} 勝率${cp.win_rate_pct}% top3率${cp.top3_rate_pct}% / ${cp.judgement || ""}`);
      }
    } catch (e) { log(`核となる車番予想の精度: エラー(${e.message})`); }

    try {
      const ready = await (await fetch(apiUrl("/purchases/investment-readiness"))).json();
      if (ready.message) {
        log(`投資判断チェック: ${ready.message}`);
      } else {
        const checks = Object.values(ready.checks || {});
        const passCount = checks.filter(c => c.pass).length;
        log(`投資判断チェック: ${passCount}/${checks.length}基準クリア / ${ready.summary || ""}`);
      }
    } catch (e) { log(`投資判断チェック: エラー(${e.message})`); }

    log("\n(各項目の詳細は下のボタンから個別に確認できます)");
  } catch (e) {
    box.textContent += "\nエラー: " + e.message;
  } finally {
    btn.disabled = false;
  }
});

// ---------- コピー・ダウンロード共通機能 ----------
// 集計結果をチャット等にすぐ貼れるように(のんの要望により追加)
function setupCopyDownload(resultBoxId, copyBtnId, downloadBtnId, filenamePrefix) {
  const box = document.getElementById(resultBoxId);
  const copyBtn = document.getElementById(copyBtnId);
  const downloadBtn = document.getElementById(downloadBtnId);
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const text = (box && (box.innerText || box.textContent)) || "";
      if (!text.trim()) { alert("コピーする内容がまだありません。先に結果を表示してください。"); return; }
      try {
        await navigator.clipboard.writeText(text);
        const orig = copyBtn.textContent;
        copyBtn.textContent = "✅ コピーしました";
        setTimeout(() => { copyBtn.textContent = orig; }, 1500);
      } catch (e) {
        alert("コピーに失敗しました: " + e.message);
      }
    });
  }
  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      const text = (box && (box.innerText || box.textContent)) || "";
      if (!text.trim()) { alert("保存する内容がまだありません。先に結果を表示してください。"); return; }
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const a = document.createElement("a");
      a.href = url;
      a.download = `${filenamePrefix}_${ts}.txt`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });
  }
}
setupCopyDownload("statsResult", "copyStatsBtn", "downloadStatsBtn", "keirin_stats");
setupCopyDownload("simResult", "copySimBtn", "downloadSimBtn", "keirin_sim");

// ---------- ④ 資金管理シミュレーション ----------

// シミュレーション欄の入力値は、開き直しても消えないようlocalStorageに保存する
// (のんの要望により追加。前回入力した値をそのまま復元する)。
const SIM_INPUT_IDS = ["simWinProb", "simOdds", "simRacePct", "simBetsPerRace", "simNumRaces", "simMaxRuinPct"];
function saveSimInputs() {
  const values = {};
  for (const id of SIM_INPUT_IDS) {
    const el = document.getElementById(id);
    if (el) values[id] = el.value;
  }
  try { localStorage.setItem("keirin_sim_inputs", JSON.stringify(values)); } catch (_) {}
}
function restoreSimInputs() {
  let values = {};
  try { values = JSON.parse(localStorage.getItem("keirin_sim_inputs") || "{}"); } catch (_) {}
  for (const id of SIM_INPUT_IDS) {
    const el = document.getElementById(id);
    if (el && values[id] !== undefined && values[id] !== "") el.value = values[id];
  }
  return Object.keys(values).length > 0;
}
for (const id of SIM_INPUT_IDS) {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", saveSimInputs);
}
const simRestored = restoreSimInputs();

// 検証タブ: 実績の的中率・シミュレーション用オッズを自動入力する。
// オッズは「全件の平均オッズ」ではなく、実績ROIと整合する的中時倍率
// (回収倍率 / 的中率) を使う。旧定義だと長期シミュレーションがほぼ全破産になる。
let simDefaultsFilled = simRestored;
let _lastSimStats = null;

function _applySimParams(params, label) {
  if (!params) return false;
  if (params.win_rate_pct !== undefined && params.win_rate_pct !== null) {
    document.getElementById("simWinProb").value = params.win_rate_pct;
  }
  if (params.odds_for_sim !== null && params.odds_for_sim !== undefined) {
    document.getElementById("simOdds").value = params.odds_for_sim;
  }
  const note = document.getElementById("simDefaultsNote");
  if (note) {
    const oldOdds = params.avg_odds_all_bets_weighted;
    note.textContent =
      `実績値を自動入力しました[${label}](的中率${params.win_rate_pct}%・シミュレーション用オッズ${params.odds_for_sim}倍、` +
      `実績収支率${params.roi_pct}%、${params.wins}/${params.n}件)。` +
      (oldOdds != null ? ` 旧・全件平均オッズ${oldOdds}倍は使いません(期待値が実績と食い違うため)。` : "") +
      " データが増えたら再読み込みしてください。";
  }
  return true;
}

function applySimScopeFromStats(data) {
  if (!data) return;
  const scopeEl = document.getElementById("simScope");
  const scope = scopeEl ? scopeEl.value : "overall";
  if (scope === "overall") {
    _applySimParams(data.sim_overall || {
      win_rate_pct: data.overall_win_rate_pct,
      odds_for_sim: data.avg_odds_weighted,
      roi_pct: data.overall_roi_pct,
      n: data.total_bets,
      wins: null,
      avg_odds_all_bets_weighted: null,
    }, "全体");
  } else {
    const byBt = data.sim_by_bet_type || {};
    const params = byBt[scope];
    if (!_applySimParams(params, scope + "のみ")) {
      const note = document.getElementById("simDefaultsNote");
      if (note) note.textContent = scope + "の実績がまだありません。全体を選ぶか、データ収集後に再読み込みしてください。";
    }
  }
}

async function fillSimDefaultsFromActuals() {
  if (simDefaultsFilled) return;
  try {
    const res = await fetch(apiUrl("/purchases/stats"));
    const data = await res.json();
    if (data.message) return;
    _lastSimStats = data;
    applySimScopeFromStats(data);
    simDefaultsFilled = true;
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("reloadSimDefaultsBtn").addEventListener("click", () => {
  simDefaultsFilled = false;
  fillSimDefaultsFromActuals();
});
const simScopeEl = document.getElementById("simScope");
if (simScopeEl) {
  simScopeEl.addEventListener("change", async () => {
    if (_lastSimStats) {
      applySimScopeFromStats(_lastSimStats);
      saveSimInputs();
      return;
    }
    simDefaultsFilled = false;
    await fillSimDefaultsFromActuals();
    saveSimInputs();
  });
}

document.getElementById("runSimBtn").addEventListener("click", async () => {
  saveSimInputs();
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

  resultBox.textContent = "シミュレーション実行中...(レース数が多い場合は最大十数秒かかることがあります)";
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 90000);
    const res = await fetch(apiUrl("/simulation/bankruptcy"), {
      signal: controller.signal,
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
    resultBox.innerHTML =
      `<p><strong>破産確率</strong>（資金が${data.ruin_threshold_pct * 100}%以下になる確率）: <strong>${data.ruin_probability_pct}%</strong></p>` +
      `<p>黒字化率（初期資金より増えて終わった割合）: ${data.profit_probability_pct}%</p>` +
      `<p>平均最終資金: ${data.average_final_bankroll}円 / 中央値: ${data.median_final_bankroll}円</p>` +
      `<p class="note">試行${data.num_trials}回` +
      (data.trials_capped ? `（計算量上限のため要求より削減。レース数が多いほど自動で間引きます）` : ``) +
      ` × ${numRaces}レース・1レース${betsPerRace}点・1レース上限${(racePct * 100).toFixed(1)}%（1点あたり${(stakeFraction * 100).toFixed(3)}%）</p>` +
      `<p class="note">この「1レース上限%」を投票プランの上限に使う場合は、下のボタンで詳細設定へ反映し、投票タブの「検証タブの投資上限を使う」をオンにしてください。</p>` +
      `<button type="button" id="applySimRacePctBtn" style="background:#16a34a;width:auto;padding:8px 14px;">この1レース上限${(racePct * 100).toFixed(0)}%を詳細設定に反映する</button>`;
    const applyBtn = document.getElementById("applySimRacePctBtn");
    if (applyBtn) {
      applyBtn.addEventListener("click", () => {
        const pct = parseFloat(document.getElementById("simRacePct").value);
        const maxInput = document.getElementById("maxRacePctInput");
        if (maxInput && !Number.isNaN(pct)) {
          maxInput.value = pct;
          if (typeof saveSettingsToStorage === "function") saveSettingsToStorage();
        }
        const useSim = document.getElementById("useSimRaceCapCheckbox");
        if (useSim) useSim.checked = true;
        alert(`1レース上限を${pct}%に反映しました。実投票のプラン作成時のみ使われます(検証集計には影響しません)。`);
      });
    }

  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});


async function runRecommendRacePct(silent) {
  const resultBox = document.getElementById("simResult");
  let bankroll;
  try {
    const bankrollRes = await fetch(apiUrl("/bankroll/"));
    const bankrollData = await bankrollRes.json();
    bankroll = bankrollData.current_balance;
  } catch (e) {
    if (!silent) resultBox.textContent = "証拠金の取得に失敗: " + e.message;
    return;
  }
  if (!bankroll) {
    if (!silent) resultBox.textContent = "証拠金残高がありません。証拠金タブで設定してください。";
    return;
  }
  const winProb = parseFloat(document.getElementById("simWinProb").value) / 100;
  const odds = parseFloat(document.getElementById("simOdds").value);
  const betsPerRace = parseInt(document.getElementById("simBetsPerRace").value) || 8;
  const numRaces = parseInt(document.getElementById("simNumRaces").value) || 20;
  const maxRuin = parseFloat(document.getElementById("simMaxRuinPct").value);
  if (Number.isNaN(maxRuin) || maxRuin < 0) {
    if (!silent) resultBox.textContent = "許容する破産確率(%)を正しく入力してください。";
    return;
  }
  if (!silent) resultBox.textContent = "許容破産確率から1レース上限%を探索中...(数十秒かかることがあります)";
  try {
    const res = await fetch(apiUrl("/simulation/recommend-race-pct"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initial_bankroll: bankroll,
        win_prob: winProb,
        odds_value: odds,
        bets_per_race: betsPerRace,
        num_races: numRaces,
        max_ruin_probability_pct: maxRuin,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    if (!data["見つかった"]) {
      resultBox.innerHTML = `<p>${data["メッセージ"] || "条件を満たす上限が見つかりませんでした。"}</p>`;
      return;
    }
    const pct = data["推奨_1レース上限%"];
    document.getElementById("simRacePct").value = pct;
    saveSimInputs();
    resultBox.innerHTML =
      (silent ? `<p class="note">📊 集計結果をもとに1レース上限%を自動計算しました。</p>` : "") +
      `<p><strong>${data["メッセージ"]}</strong></p>` +
      `<p>その上限での破産確率: ${data["その上限での破産確率%"]}% ／ 黒字化率: ${data["黒字化率%"]}%</p>` +
      `<p>平均最終資金: ${data["平均最終資金"]}円</p>` +
      `<p class="note">下のボタンで詳細設定へ反映し、実投票のプラン作成時のみ使います。</p>` +
      `<button type="button" id="applySimRacePctBtn" style="background:#16a34a;width:auto;padding:8px 14px;">この1レース上限${pct}%を詳細設定に反映する</button>`;
    const applyBtn = document.getElementById("applySimRacePctBtn");
    if (applyBtn) {
      applyBtn.addEventListener("click", () => {
        const maxInput = document.getElementById("maxRacePctInput");
        if (maxInput) {
          maxInput.value = pct;
          if (typeof saveSettingsToStorage === "function") saveSettingsToStorage();
        }
        const useSim = document.getElementById("useSimRaceCapCheckbox");
        if (useSim) useSim.checked = true;
        alert(`1レース上限を${pct}%に反映しました。実投票のプラン作成時のみ使われます。`);
      });
    }
  } catch (e) {
    if (!silent) resultBox.textContent = "エラー: " + e.message;
  }
}

document.getElementById("recommendRacePctBtn").addEventListener("click", () => { saveSimInputs(); runRecommendRacePct(false); });


// ---------- ⑤ 実績検証 ----------
function renderBucketTable(title, bucketObj) {
  if (!bucketObj || Object.keys(bucketObj).length === 0) return "";
  let html = `<p style="margin-top:10px;"><strong>${title}</strong></p><table><tr><th>区分</th><th>件数</th><th>的中率</th><th>想定的中率</th><th>実績</th><th>想定回収率</th></tr>`;
  for (const [key, v] of Object.entries(bucketObj)) {
    const cls = v.expectancy_pct > 0 ? "ev-positive" : "";
    html += `<tr class="${cls}"><td>${key}</td><td>${v.count}</td><td>${v.win_rate_pct}%</td><td>${v.expected_win_rate_pct ?? "-"}${v.expected_win_rate_pct !== null ? "%" : ""}</td><td>${v.expectancy_pct ?? "-"}${v.expectancy_pct !== null ? "%" : ""}</td><td>${v.expected_roi_pct ?? "-"}${v.expected_roi_pct !== null ? "%" : ""}</td></tr>`;
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

    // 集計結果をもとに、シミュレーションの勝率・オッズを最新値に更新し、
    // 1レースあたりの投資上限も自動計算しておく。オッズは実績ROI整合値を使用。
    _lastSimStats = data;
    applySimScopeFromStats(data);
    saveSimInputs();
    runRecommendRacePct(true);
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

    if (data.effectiveness) {
      const e = data.effectiveness;
      const ok = e.improvement_pt > 0.5;
      const color = ok ? "#16a34a" : (e.improvement_pt > -0.5 ? "#f59e0b" : "#ef4444");
      const judge = e["判定"] || e.判定 || "";
      const desc = e["説明"] || e.説明 || "";
      html += `<div style="border:1px solid ${color};border-radius:8px;padding:12px;margin-bottom:12px;">`;
      html += `<p style="margin:0 0 6px 0;"><strong>① 補正の効き（同じ購入への before/after）</strong></p>`;
      html += `<p style="margin:0;">補正前の乖離: <strong>${e.before_deviation_pt}pt</strong> → 補正後: <strong>${e.after_deviation_pt}pt</strong></p>`;
      html += `<p style="margin:6px 0;color:${color};font-size:1.1em;"><strong>縮んだ量: ${e.improvement_pt > 0 ? "+" : ""}${e.improvement_pt}pt</strong> ／ ${judge}</p>`;
      html += `<p class="note" style="margin:0;">対象 ${e.n}件。${desc}</p>`;
      html += `</div>`;
    } else {
      html += `<p class="note">① 補正の効き: raw確率がある購入がまだ無いため表示できません。</p>`;
    }

    html += `<div style="border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:12px;">`;
    html += `<p style="margin:0 0 8px 0;"><strong>② 直近の購入精度（購入時点の勝率 vs 実績）</strong></p>`;
    html += `<p class="note" style="margin:0 0 8px 0;">レース開催日ベースです（取込日ではありません）。最近開催分の購入精度を見ます。</p>`;
    html += `<table><tr><th>期間</th><th>件数</th><th>的中</th><th>実績的中率</th><th>予想平均</th><th>乖離</th></tr>`;
    const recent = data.recent || {};
    for (const key of ["直近3日", "直近7日", "直近14日"]) {
      const r = recent[key];
      if (!r || !r.sample_count) {
        html += `<tr><td>${key}</td><td colspan="5">${(r && (r["メッセージ"] || r.メッセージ)) || "データなし"}</td></tr>`;
        continue;
      }
      const sign = r.deviation_pct > 0 ? "+" : "";
      const cls = Math.abs(r.deviation_pct) >= 5 ? ' style="color:#f59e0b;font-weight:bold;"' : "";
      html += `<tr><td>${key}</td><td>${r.sample_count}</td><td>${r.wins ?? "-"}</td>`;
      html += `<td>${r.actual_win_rate_pct}%</td><td>${r.predicted_avg_prob_pct}%</td>`;
      html += `<td${cls}>${sign}${r.deviation_pct}pt</td></tr>`;
    }
    html += `</table></div>`;

    if (data.factor_overall) {
      const f = data.factor_overall;
      html += `<p><strong>全体補正係数: ${f.calibration_factor}倍</strong>`;
      html += `(学習用: 実績${f.actual_win_rate_pct}% / 想定${f.predicted_avg_prob_pct}%、${f.sample_count}件)</p>`;
    }
    if (data.message) html += `<p class="note">${data.message}</p>`;

    if (data.overall) {
      const o = data.overall;
      const sign = o.deviation_pct > 0 ? "+" : "";
      html += `<details style="margin-top:10px;"><summary class="note">参考: 全期間購入の乖離（数日ではほぼ動きません）</summary>`;
      html += `<p>全期間: ${sign}${o.deviation_pct}pt（実績${o.actual_win_rate_pct}% − 予想${o.predicted_avg_prob_pct}%、${o.sample_count}件）</p>`;
      html += `<p class="note">判断は①②を優先してください。</p></details>`;
    }

    html += "<p style=\"margin-top:12px;\">勝率帯ごとの係数内訳:</p>";
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
        const cls = info.significance_p_value_pct < 5 ? ' style="color:#ef4444;font-weight:bold;"' : (info.significance_p_value_pct < 20 ? ' style="color:#f59e0b;"' : "");
        pText = `<span${cls}>${info.significance_p_value_pct}%</span>`;
      }
      const sc = info.sample_count != null
        ? `${info.sample_count}(購入${info.purchase_count || 0}+見送り${info.skipped_count || 0})`
        : "-";
      html += `<tr><td>${bucket}</td><td>${sc}</td><td>${info.required_sample_count}</td><td>${status}</td>`;
      html += `<td>${info.actual_win_rate_pct != null ? info.actual_win_rate_pct + "%" : "-"}</td>`;
      html += `<td>${info.predicted_avg_prob_pct != null ? info.predicted_avg_prob_pct + "%" : "-"}</td>`;
      html += `<td>${devText}</td><td>${pText}</td><td>${info.calibration_factor}倍</td></tr>`;
    }
    html += "</table>";

    if (data.by_bet_type_bucket && Object.keys(data.by_bet_type_bucket).length) {
      html += `<p style="margin-top:12px;"><strong>券種×勝率帯の交差係数</strong></p>`;
      html += `<table><tr><th>券種</th><th>勝率帯</th><th>試行数</th><th>必要数</th><th>実績的中率</th><th>予想平均</th><th>ズレ</th><th>偶然の確率</th><th>補正係数</th></tr>`;
      for (const [bt, bands] of Object.entries(data.by_bet_type_bucket)) {
        // APIは { "3連単": { "0-5%(大穴)": {sample_count...} } } の入れ子
        if (!bands || typeof bands !== "object" || bands.sample_count != null) {
          // 旧形式(フラット)のフォールバック
          const info = bands;
          if (!info || info.sample_count == null) continue;
          html += `<tr><td>${bt}</td><td></td><td>${info.sample_count}</td><td>${info.required_sample_count}</td>`;
          html += `<td>${info.actual_win_rate_pct != null ? info.actual_win_rate_pct + "%" : "-"}</td>`;
          html += `<td>${info.predicted_avg_prob_pct != null ? info.predicted_avg_prob_pct + "%" : "-"}</td>`;
          const sign = (info.deviation_pct != null && info.deviation_pct > 0) ? "+" : "";
          html += `<td>${info.deviation_pct != null ? sign + info.deviation_pct + "pt" : "-"}</td>`;
          html += `<td>${info.significance_p_value_pct != null ? info.significance_p_value_pct + "%" : "-"}</td>`;
          html += `<td>${info.calibration_factor}倍</td></tr>`;
          continue;
        }
        for (const [band, info] of Object.entries(bands)) {
          if (!info || info.sample_count == null) continue;
          const sign = (info.deviation_pct != null && info.deviation_pct > 0) ? "+" : "";
          html += `<tr><td>${bt}</td><td>${band}</td><td>${info.sample_count}</td><td>${info.required_sample_count}</td>`;
          html += `<td>${info.actual_win_rate_pct != null ? info.actual_win_rate_pct + "%" : "-"}</td>`;
          html += `<td>${info.predicted_avg_prob_pct != null ? info.predicted_avg_prob_pct + "%" : "-"}</td>`;
          html += `<td>${info.deviation_pct != null ? sign + info.deviation_pct + "pt" : "-"}</td>`;
          html += `<td>${info.significance_p_value_pct != null ? info.significance_p_value_pct + "%" : "-"}</td>`;
          html += `<td>${info.calibration_factor}倍</td></tr>`;
        }
      }
      html += "</table>";
    }

    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});

document.getElementById("loadCalibrationCompareBtn").addEventListener("click", async () => {
  const box = document.getElementById("statsResult");
  box.textContent = "キャリブレーション比較を読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/calibration-compare"));
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    const enabled = loadCalAxisEnabled();
    let html = `<p>対象レコード ${data.n_records}件 ／ 補正前確率なし(旧データ) ${data.n_without_raw}件<br>${data.note || ""}</p>`;
    if (data.overall) {
      const ob = data.overall.before || {};
      const oa = data.overall.after || {};
      html += `<div style="background:#1e3a5f;padding:10px;border-radius:8px;margin:8px 0;">
        <strong>📈 総括(実際に購入した分のみ)</strong><br>
        補正前の乖離: ${ob.deviation_pt ?? "-"}pt(予想精度${ob.accuracy_pct ?? "-"}%)<br>
        補正後の乖離: <strong>${oa.deviation_pt ?? "-"}pt</strong>(予想精度${oa.accuracy_pct ?? "-"}%)<br>
        実績収支率(実際に購入した分): ${data.overall.actual_roi_pct ?? "-"}${data.overall.actual_roi_pct != null ? "%" : ""}(${data.overall.n_purchased}件)
      </div>`;
    }
    html += `<p class="note">各軸のチェックは表示のオンオフです。勝率帯の自動補正そのもののオンオフは、下の「自動補正をプランに適用する」を使います。</p>`;
    html += `<p class="note">📌 判断は主に「乖離(ポイント)」と「実績収支率」(実際に購入した分のみ)で見てください。p値は件数が多いほど、ごくわずかなズレでも「有意」と出やすくなるため、件数が多い条件では0%に近くなりがちです(判断の補助情報として参考程度に)。</p>`;
    for (const [axis, rows] of Object.entries(data.axes || {})) {
      const on = enabled[axis] !== false;
      html += `<h3>${axis} <label style="font-weight:normal;font-size:13px;"><input type="checkbox" class="calAxisToggle" data-axis="${axis}" ${on ? "checked" : ""}> この軸を表示</label></h3>`;
      if (!on) {
        html += `<p class="note">(非表示)</p>`;
        continue;
      }
      html += `<table><tr>
        <th>条件</th><th>件数</th><th>補正前あり</th>
        <th style="background:#1e3a5f;">実績収支率%<br><span style="font-weight:normal;font-size:11px;">(実際に購入した分)</span></th>
        <th style="background:#1e3a5f;">補正後・乖離(pt)</th>
        <th>補正前・予想精度%</th><th>補正前・乖離(pt)</th><th style="color:#888;">補正前・p値%</th>
        <th>補正後・予想精度%</th><th style="color:#888;">補正後・p値%</th>
        <th>補正の効果</th></tr>`;
      for (const row of rows) {
        const b = row.before || {};
        const a = row.after || {};
        const imp = row.calibration_improved == null ? "-" : (row.calibration_improved ? "改善" : "悪化または同等");
        const devAbs = a.deviation_pt == null ? null : Math.abs(a.deviation_pt);
        const devCls = devAbs == null ? "" : (devAbs <= 1 ? ' style="color:#4ade80;font-weight:bold;"' : (devAbs <= 3 ? ' style="color:#f59e0b;font-weight:bold;"' : ' style="color:#ef4444;font-weight:bold;"'));
        const roiCls = row.actual_roi_pct == null ? "" : (row.actual_roi_pct >= 100 ? ' style="color:#4ade80;font-weight:bold;"' : ' style="color:#ef4444;font-weight:bold;"');
        html += `<tr><td>${row.bucket}</td><td>${row.n_total}</td><td>${row.n_with_raw}</td>
          <td${roiCls}>${row.actual_roi_pct ?? "-"}${row.actual_roi_pct != null ? "%" : ""}<br><span style="font-weight:normal;font-size:11px;">(${row.n_purchased}件)</span></td>
          <td${devCls}>${a.deviation_pt ?? "-"}</td>
          <td>${b.accuracy_pct ?? "-"}</td><td>${b.deviation_pt ?? "-"}</td><td style="color:#888;">${b.p_value_pct ?? "-"}</td>
          <td>${a.accuracy_pct ?? "-"}</td><td style="color:#888;">${a.p_value_pct ?? "-"}</td>
          <td>${imp}</td></tr>`;
      }
      html += `</table>`;
    }
    box.innerHTML = html;
    box.querySelectorAll(".calAxisToggle").forEach((el) => {
      el.addEventListener("change", () => {
        const map = loadCalAxisEnabled();
        map[el.dataset.axis] = el.checked;
        localStorage.setItem("keirin_cal_axis_enabled", JSON.stringify(map));
      });
    });
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
});

function loadCalAxisEnabled() {
  try {
    return JSON.parse(localStorage.getItem("keirin_cal_axis_enabled") || "{}");
  } catch (_) {
    return {};
  }
}

function isCalibrationApplyEnabled() {
  const el = document.getElementById("applyCalibrationCheckbox");
  return !el || el.checked;
}






document.getElementById("loadProfitConcentrationBtn").addEventListener("click", async () => {
  const box = document.getElementById("statsResult");
  box.textContent = "利益の集中度を集計中...";
  try {
    const res = await fetch(apiUrl("/purchases/profit-concentration"));
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    if (data.message) {
      box.textContent = data.message;
      return;
    }
    const g = data["概要"] || {};
    const d = data["的中オッズの分布"] || {};
    const c = data["集中度"] || {};
    let html = "<h3>概要</h3><ul>";
    html += `<li>総ベット数: ${g["総ベット数"]}（的中 ${g["的中件数"]} / 外れ ${g["不的中件数"]}）</li>`;
    html += `<li>総投資額: ${g["総投資額"]}円 / 総払戻: ${g["総払戻"]}円 / 総損益: ${g["総損益"]}円</li>`;
    html += "</ul>";

    html += "<h3>的中した買い目のオッズ分布</h3><ul>";
    html += `<li>中央値: ${d["中央値倍"] ?? "-"}倍 ／ 平均: ${d["平均倍"] ?? "-"}倍</li>`;
    html += `<li>75%点: ${d["75%点倍"] ?? "-"}倍 ／ 90%点: ${d["90%点倍"] ?? "-"}倍 ／ 最大: ${d["最大倍"] ?? "-"}倍</li>`;
    html += `<li>最小: ${d["最小倍"] ?? "-"}倍（オッズ判明 ${d["件数_オッズ判明"] ?? 0}件）</li>`;
    html += "</ul>";

    html += "<h3>集中度</h3><ul>";
    html += `<li>的中上位5件 → 全体利益の ${c["的中上位5件が全体利益に占める割合%"] ?? "-"}%</li>`;
    html += `<li>的中上位10件 → 全体利益の ${c["的中上位10件が全体利益に占める割合%"] ?? "-"}%</li>`;
    html += `<li>的中上位10件 → 的中払戻の ${c["的中上位10件が的中払戻に占める割合%"] ?? "-"}%</li>`;
    html += `<li>100倍以上の的中 → 全体利益の ${c["100倍以上の的中が全体利益に占める割合%"] ?? "-"}%</li>`;
    html += `<li>黒字レース: ${c["黒字レース数"] ?? "-"} / ${c["対象レース数"] ?? "-"}（${c["黒字レース割合%"] ?? "-"}%）</li>`;
    html += `<li>利益上位5レース → 全体利益の ${c["利益上位5レースが全体利益に占める割合%"] ?? "-"}%</li>`;
    html += "</ul>";

    html += "<h3>判定</h3><ul>";
    for (const line of (data["判定"] || [])) {
      html += `<li>${line}</li>`;
    }
    html += "</ul>";

    const band = data["的中オッズ帯別"] || {};
    html += "<h3>的中オッズ帯別</h3>";
    html += "<table><tr><th>オッズ帯</th><th>的中件数</th><th>払戻合計</th><th>利益合計</th><th>利益の割合%</th></tr>";
    for (const [k, v] of Object.entries(band)) {
      html += `<tr><td>${k}</td><td>${v["的中件数"]}</td><td>${v["払戻合計"]}</td><td>${v["利益合計"]}</td><td>${v["利益が全体利益に占める割合%"] ?? "-"}</td></tr>`;
    }
    html += "</table>";

    const bet = data["的中の券種別"] || {};
    html += "<h3>的中の券種別</h3>";
    html += "<table><tr><th>券種</th><th>的中件数</th><th>払戻合計</th><th>利益合計</th></tr>";
    for (const [k, v] of Object.entries(bet)) {
      html += `<tr><td>${k}</td><td>${v["的中件数"]}</td><td>${v["払戻合計"]}</td><td>${v["利益合計"]}</td></tr>`;
    }
    html += "</table>";

    const pb = data["的中の想定勝率帯別"] || {};
    html += "<h3>的中の想定勝率帯別</h3>";
    html += "<table><tr><th>勝率帯</th><th>的中件数</th><th>払戻合計</th><th>利益合計</th></tr>";
    for (const [k, v] of Object.entries(pb)) {
      html += `<tr><td>${k}</td><td>${v["的中件数"]}</td><td>${v["払戻合計"]}</td><td>${v["利益合計"]}</td></tr>`;
    }
    html += "</table>";

    const tops = data["利益の大きい的中_上位"] || [];
    html += "<h3>利益の大きい的中（上位）</h3>";
    html += "<table><tr><th>レースID</th><th>券種</th><th>買い目</th><th>オッズ</th><th>投資</th><th>払戻</th><th>利益</th><th>想定勝率%</th></tr>";
    for (const r of tops) {
      html += `<tr><td>${r["レースID"]}</td><td>${r["券種"]}</td><td>${r["買い目"]}</td><td>${r["オッズ"] ?? "-"}</td><td>${r["投資額"]}</td><td>${r["払戻"]}</td><td>${r["利益"]}</td><td>${r["購入時想定勝率%"] ?? "-"}</td></tr>`;
    }
    html += "</table>";
    html += `<p class="note">目安: 的中上位10件で利益の50%以上、または100倍以上の的中で利益の50%以上なら「高オッズ依存が強い」と判断しやすいです。</p>`;
    box.innerHTML = html;
  } catch (e) {
    box.textContent = "エラー: " + e.message;
  }
});

document.getElementById("loadCarPickBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/car-pick-accuracy"));
    const data = await res.json();
    if (data.message) {
      resultBox.textContent = data.message;
      return;
    }
    const cls = data.significance_p_value_pct < 5 ? ' style="color:#ef4444;font-weight:bold;"' : (data.significance_p_value_pct < 20 ? ' style="color:#f59e0b;"' : "");
    let html = `<p class="note">券種の組み合わせを介さず、「そのレースでAIが最も勝つと見積もった車番」だけを追跡した、1レース=1試行の指標です。</p>`;
    html += `<p><strong>${data.n_races}レース中 ${data.win_count}レースで1着的中(${data.win_rate_pct}%)</strong> / 上位3着以内: ${data.top3_count}レース(${data.top3_rate_pct}%)</p>`;
    html += `<p>想定平均勝率: ${data.avg_predicted_win_prob_pct}%</p>`;
    html += `<p${cls}>📊 このズレが偶然起きる確率: ${data.significance_p_value_pct}%(${data.judgement})</p>`;
    html += `<table><tr><th>レース</th><th>本命車番</th><th>選手名</th><th>想定勝率</th><th>着順</th><th>1着</th><th>3着以内</th></tr>`;
    for (const it of data.items) {
      const cls2 = it.won ? "ev-positive" : (it.in_top3 ? "" : "ev-skip");
      html += `<tr class="${cls2}"><td>${it.venue_name}${it.race_number}R</td><td>${it.top_pick_car_number}番</td><td>${it.top_pick_player_name}</td><td>${it.predicted_win_prob_pct}%</td><td>${it.actual_result}</td><td>${it.won ? "🟢" : "-"}</td><td>${it.in_top3 ? "🟢" : "-"}</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
  } catch (e) {
    resultBox.textContent = "エラー: " + e.message;
  }
});


document.getElementById("loadReadinessBtn").addEventListener("click", async () => {
  const resultBox = document.getElementById("statsResult");
  resultBox.textContent = "読み込み中...";
  try {
    const res = await fetch(apiUrl("/purchases/investment-readiness"));
    const data = await res.json();
    if (data.message) {
      resultBox.textContent = data.message;
      return;
    }
    const labels = {
      sample_size: "① サンプル数は十分か",
      calibration: "② 予想と実績のズレは偶然の範囲か",
      profitability: "③ 収支は安定してプラスか",
      bankruptcy_risk: "④ 破産確率は十分低いか",
    };
    let html = `<p style="font-size:16px;"><strong>${data.ready ? "✅" : "⏳"} ${data.summary}</strong></p>`;
    html += `<table><tr><th>基準</th><th>判定</th><th>詳細</th></tr>`;
    for (const [key, label] of Object.entries(labels)) {
      const c = data.checks[key];
      html += `<tr class="${c.pass ? "ev-positive" : ""}"><td>${label}</td><td>${c.pass ? "🟢達成" : "未達"}</td><td>${c.detail}</td></tr>`;
    }
    html += "</table>";
    resultBox.innerHTML = html;
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
setRaceFilterButtons("today");
loadRaces();
refreshBankrollDisplay();
