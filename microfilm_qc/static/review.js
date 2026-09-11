/* 二次验收（抽查复核）模块：可复现三段均衡抽样、强制纳入、版本锁定、
   匿名审片、失败门限、自动加抽/退回、作废记录与移交导出。
   依赖 app.js 暴露的全局 $ / api / post / toast / state / loadState / renderAll。*/
"use strict";

const rv = {
  detail: null,        // 当前打开的轮次 detail（匿名或审计视图）
  pos: 0,              // 审片位置（在 items 中）
  mode: "anon",        // anon（匿名审片） / audit（非匿名证据）
  busy: false,
};

const RV_DIMS = [
  ["clarity", "清晰度"], ["crop", "裁边"], ["orientation", "朝向"],
  ["blemish", "污损"], ["missing", "内容缺失"],
];
const RV_STATUS = {
  open: ["复核中", "rv-open"], passed: ["通过", "rv-pass"],
  failed: ["未通过", "rv-fail"], returned: ["已退回", "rv-ret"],
};
const RV_SEG = { head: "首段", middle: "中段", tail: "末段", forced: "强制" };

/* ---------------- 主面板 ---------------- */
function rvSummary() { return state.data && state.data.review; }

function rvRender() {
  const sum = rvSummary();
  const box = $("#rvRounds");
  if (!box) return;
  $("#rvRoundCount").textContent = sum && sum.rounds.length ? sum.rounds.length : "";
  // 默认门限回填
  if (sum && $("#rvDefaultLimit").value === "") $("#rvDefaultLimit").value = sum.default_fail_limit;
  // 失败门限默认跟随
  if (sum && !$("#rvFailLimit").dataset.touched) $("#rvFailLimit").value = sum.default_fail_limit;

  const startForm = $("#rvStartForm");
  const startBtn = $("#btnRvStart");
  const hasOpen = sum && sum.open_round_id;
  startBtn.disabled = !!hasOpen;
  startBtn.textContent = hasOpen ? "有复核中的轮次" : "发起抽样";
  startForm.classList.toggle("hidden", !hasOpen && startForm.dataset.open !== "1");

  box.innerHTML = "";
  if (!sum || !sum.rounds.length) {
    box.innerHTML = "<p class='hint'>尚无抽查轮次。发起抽样后，系统锁定图像版本并生成匿名审片清单。</p>";
    return;
  }
  for (const r of sum.rounds.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "rv-card";
    const [slabel, scls] = RV_STATUS[r.status] || [r.status, ""];
    const p = r.progress;
    const scheme = r.mode === "ratio"
      ? `占比 ${(r.ratio * 100).toFixed(0)}%` : `数量 ${r.count}`;
    const verNote = (r.status === "passed" && r.versions_current === false)
      ? "<span class='rv-warn'>⚠ 锁定版本已被更换，通过结论失效</span>" : "";
    card.innerHTML = `
      <div class="rv-card-head">
        <b>第 ${r.seq} 轮</b>
        <span class="rv-badge ${scls}">${slabel}</span>
        <span class="hint">${scheme} ｜ 门限 ${r.fail_limit} ｜ 种子 <code>${r.seed}</code></span>
        <span class="spacer"></span>
        <span class="hint">复核人 ${r.reviewer}</span>
      </div>
      <div class="hint">总体 ${r.pool_size} ｜ 随机 ${r.n_random}（首 ${r.n_head}/中 ${r.n_middle}/末 ${r.n_tail}）
        ｜ 强制 ${r.n_forced} ｜ 已判合格 ${p.pass}、不合格 ${p.fail}、作废 ${p.void}、待判 ${p.pending}</div>
      ${r.conclusion ? `<div class="rv-concl">${r.conclusion}</div>` : ""}
      ${verNote}
      <div class="rv-card-actions"></div>`;
    const acts = card.querySelector(".rv-card-actions");
    if (r.status === "open") {
      const b = document.createElement("button");
      b.className = "primary";
      b.textContent = p.all_decided ? "查看结论 / 结束本轮" : "进入匿名审片";
      b.addEventListener("click", () => rvOpen(r.id));
      acts.appendChild(b);
    } else {
      const b = document.createElement("button");
      b.className = "ghost";
      b.textContent = "查看证据（非匿名）";
      b.addEventListener("click", () => rvAudit(r.id));
      acts.appendChild(b);
    }
    box.appendChild(card);
  }
}

/* ---------------- 发起抽样 ---------------- */
async function rvStart() {
  const body = {
    reviewer: $("#rvReviewer").value.trim(),
    mode: $("#rvMode").value,
    count: +$("#rvCount").value || 0,
    ratio: +$("#rvRatio").value || 0,
    seed: $("#rvSeed").value.trim(),
    fail_limit: +$("#rvFailLimit").value || 0,
  };
  try {
    const j = await post(`/api/reels/${state.reelId}/reviews/start`, body);
    state.data = j.state;
    $("#rvStartForm").dataset.open = "";
    renderAll();
    rvOpen(j.round_id);
  } catch (e) { /* toast */ }
}

/* ---------------- 匿名审片 ---------------- */
async function rvOpen(roundId) {
  try {
    rv.detail = await api(`/api/reviews/${roundId}`);
  } catch (e) { return; }
  rv.mode = "anon";
  rv.pos = rv.detail.items.findIndex((it) => it.status === "pending");
  if (rv.pos < 0) rv.pos = 0;
  $("#rvModal").classList.remove("hidden");
  $("#btnRvPassRound").classList.remove("hidden");
  $("#btnRvExtend").classList.remove("hidden");
  $("#btnRvReturn").classList.remove("hidden");
  rvRenderCurrent();
}

function rvEffectiveItems() { return rv.detail.items; }

function rvRenderCurrent() {
  if (rv.mode === "audit") rvRenderAudit(rv.detail);
  else rvRenderViewer();
}

function rvRenderViewer() {
  const d = rv.detail, r = d.round;
  $("#rvRoundMeta").textContent =
    `第 ${r.seq} 轮 ｜ 种子 ${r.seed} ｜ 门限 ${r.fail_limit} ｜ 复核人 ${r.reviewer}`;
  const p = d.progress;
  $("#rvProgress").textContent =
    `合格 ${p.pass} · 不合格 ${p.fail} · 作废 ${p.void} · 待判 ${p.pending}`;
  const items = rvEffectiveItems();
  if (rv.pos >= items.length) rv.pos = items.length - 1;
  if (rv.pos < 0) rv.pos = 0;
  const it = items[rv.pos];
  $("#rvIndex").textContent = `第 ${rv.pos + 1} / ${items.length} 张`;
  // 匿名图像：仅锁定版本图，不暴露编号/文件名
  $("#rvImage").src = `/api/review-items/${it.id}/image?w=1000&_=${it.id}`;

  const isVoid = it.status === "void";
  $("#rvVoidNote").classList.toggle("hidden", !isVoid);
  $("#rvVoidNote").textContent = isVoid
    ? `本记录已作废，不计入本轮结论：${it.void_reason || "复核期间图像被更换"}。审阅历史保留。` : "";
  // 载入已有判定（允许改判，历史保留）；未判定时五项必须逐项明确选择
  const dims = it.dims || {};
  const locked = isVoid || rv.mode === "audit";
  document.querySelectorAll("#rvDims .rv-dim-row").forEach((row) => {
    const key = row.dataset.dim;
    const radios = row.querySelectorAll("input[type=radio]");
    radios.forEach((rb) => { rb.checked = false; rb.disabled = locked; });
    if (dims[key] !== undefined && dims[key] !== null) {
      const want = dims[key] ? "1" : "0";
      const sel = row.querySelector(`input[value="${want}"]`);
      if (sel) sel.checked = true;
    }
    row.classList.toggle("rv-unset",
      !locked && (dims[key] === undefined || dims[key] === null));
  });
  $("#rvNote").value = it.note || "";
  $("#rvNote").disabled = locked;
  $("#rvReshoot").checked = !!it.transfer_reshoot;
  $("#rvReshoot").disabled = locked;
  const interactive = !locked;
  $("#btnRvPass").disabled = !interactive;
  $("#btnRvFail").disabled = !interactive;
  $("#btnRvPrev").disabled = rv.pos === 0;
  $("#btnRvNext").disabled = rv.pos === items.length - 1;
  if (rv.mode === "anon") rvSyncJudgeButtons();

  // 全部判定后展示结论区
  const doneBox = $("#rvDone");
  if (p.all_decided) {
    doneBox.classList.remove("hidden");
    const overLimit = p.fail > r.fail_limit;
    $("#rvDoneStat").innerHTML =
      `有效 ${p.effective} 张：合格 ${p.pass}、不合格 ${p.fail}、作废 ${p.void}。` +
      (overLimit
        ? `<span class="rv-fail">失败数超过门限 ${r.fail_limit}，不能通过；请自动加抽或退回整卷。</span>`
        : `<span class="rv-pass">失败数未超过门限，可以通过。</span>`);
    $("#btnRvPassRound").disabled = overLimit;
  } else {
    doneBox.classList.add("hidden");
  }
}

/* 读取五项判定：返回 {dims, unset:[], bad:[]}；
   未选择的项计入 unset（区别于明确的“不合格”）。 */
function rvReadDims() {
  const dims = {}, unset = [];
  document.querySelectorAll("#rvDims .rv-dim-row").forEach((row) => {
    const key = row.dataset.dim;
    const sel = row.querySelector("input[type=radio]:checked");
    if (!sel) { unset.push(key); dims[key] = null; }
    else dims[key] = sel.value === "1";
  });
  const bad = RV_DIMS.filter(([k]) => dims[k] === false).map(([k]) => k);
  return { dims, unset, bad };
}

/* 五项明确且全部合格才可直接判合格；存在明确不合格项才可判不合格 */
function rvSyncJudgeButtons() {
  const { dims, unset, bad } = rvReadDims();
  const decidedAll = unset.length === 0;
  $("#btnRvPass").disabled = !decidedAll || bad.length > 0;
  $("#btnRvFail").disabled = !decidedAll || bad.length === 0;
}

async function rvJudge(verdict) {
  if (rv.busy) return;
  const d = rv.detail;
  const it = rvEffectiveItems()[rv.pos];
  const { dims, unset } = rvReadDims();
  if (unset.length) {
    const names = RV_DIMS.filter(([k]) => unset.includes(k)).map(([, n]) => n).join("、");
    return toast(`请对 ${names} 明确选择合格/不合格`, true);
  }
  const note = $("#rvNote").value;
  if (verdict === "fail" && !note.trim()) {
    return toast("不合格项必须填写备注", true);
  }
  rv.busy = true;
  try {
    const j = await post(`/api/review-items/${it.id}/judge`, {
      reviewer: d.round.reviewer, verdict, dims, note,
      transfer_reshoot: $("#rvReshoot").checked,
    });
    rv.detail = j.detail;
    // 自动前进到下一个待判定
    const next = rv.detail.items.findIndex((x, i) => i > rv.pos && x.status === "pending");
    rv.pos = next >= 0 ? next : Math.min(rv.pos, rv.detail.items.length - 1);
    rvRenderViewer();
    toast(verdict === "fail" ? "已记录不合格（历史保留）" : "已记录合格");
  } catch (e) { /* toast */ } finally { rv.busy = false; }
}

async function rvFinish(action) {
  const r = rv.detail.round;
  let conclusion = "";
  if (action === "return") {
    conclusion = prompt("退回整卷缘由（将记入移交与审计）：", "");
    if (!conclusion || !conclusion.trim()) return;
  } else if (action === "pass") {
    if (!confirm("确认本轮二次验收通过？通过结论将作为定稿门禁依据。")) return;
  }
  try {
    const j = await post(`/api/reviews/${r.id}/finish`, {
      reviewer: r.reviewer, action, conclusion,
    });
    rv.detail = j.detail;
    state.data = j.state;
    renderAll();
    $("#rvModal").classList.add("hidden");
    toast(action === "pass" ? "本轮通过，可定稿移交" : "已退回整卷");
  } catch (e) { if (e.detail) { rv.detail = e.detail; rvRenderCurrent(); } }
}

async function rvExtend() {
  const r = rv.detail.round;
  if (!confirm("失败数超过门限，将在同一链上发起自动加抽（新一轮可复现抽样）。继续？")) return;
  const count = prompt("加抽数量（张，不含强制项）：", String(r.count || 9));
  if (count == null) return;
  try {
    const j = await post(`/api/reviews/${r.id}/extend`, {
      mode: "count", count: +count || 0,
      seed: (Math.random().toString(16).slice(2, 10)),
    });
    state.data = j.state;
    renderAll();
    rvOpen(j.round_id);
    toast("已生成加抽轮次，上一轮标记为未通过");
  } catch (e) { /* toast */ }
}

/* ---------------- 非匿名审计视图 ---------------- */
async function rvAudit(roundId) {
  let d;
  try { d = await api(`/api/reviews/${roundId}/audit`); } catch (e) { return; }
  rv.detail = d;
  rv.mode = "audit";
  rv.pos = 0;
  $("#rvModal").classList.remove("hidden");
  rvRenderAudit(d);
}

function rvRenderAudit(d) {
  const r = d.round;
  $("#rvRoundMeta").textContent =
    `证据视图（非匿名）第 ${r.seq} 轮 ｜ 种子 ${r.seed} ｜ 复核人 ${r.reviewer}`;
  $("#rvProgress").textContent = `合格 ${d.progress.pass} · 不合格 ${d.progress.fail} · 作废 ${d.progress.void}`;
  const it = d.items[rv.pos] || {};
  $("#rvIndex").textContent = `第 ${(rv.pos + 1)} / ${d.items.length} 张（含身份信息）`;
  $("#rvImage").src = `/api/review-items/${it.id}/image?w=1000`;
  const tags = (it.forced_labels || []).join("、");
  $("#rvVoidNote").classList.remove("hidden");
  $("#rvVoidNote").innerHTML =
    `帧号 <b>No.${it.frame_no}</b> ｜ 文件 ${it.filename || "（已删除）"}` +
    ` ｜ 落区 ${RV_SEG[it.segment] || it.segment} ｜ 入选 ${
      { random: "随机", forced: "强制", both: "随机+强制" }[it.selected_by] || it.selected_by}` +
    (tags ? ` ｜ 强制原因：${tags}` : "") +
    (it.status === "void" ? `<br><span class="rv-fail">已作废：${it.void_reason}</span>` : "") +
    (it.note ? `<br>备注：${it.note}` : "");
  const dims = it.dims || {};
  document.querySelectorAll("#rvDims .rv-dim-row").forEach((row) => {
    const key = row.dataset.dim;
    const radios = row.querySelectorAll("input[type=radio]");
    radios.forEach((rb) => { rb.disabled = true; rb.checked = false; });
    if (dims[key] !== undefined && dims[key] !== null) {
      const sel = row.querySelector(`input[value="${dims[key] ? 1 : 0}"]`);
      if (sel) sel.checked = true;
    }
  });
  $("#rvNote").value = it.note || ""; $("#rvNote").disabled = true;
  $("#rvReshoot").checked = !!it.transfer_reshoot; $("#rvReshoot").disabled = true;
  $("#btnRvPass").disabled = true; $("#btnRvFail").disabled = true;
  $("#btnRvPrev").disabled = rv.pos === 0;
  $("#btnRvNext").disabled = rv.pos === d.items.length - 1;
  $("#rvDone").classList.remove("hidden");
  $("#rvDoneStat").textContent = "证据只读视图。判定：" +
    ({ pass: "合格", fail: "不合格", void: "已作废", pending: "未判定" }[it.status] || it.status);
  $("#btnRvPassRound").classList.add("hidden");
  $("#btnRvExtend").classList.add("hidden");
  $("#btnRvReturn").classList.add("hidden");
}

/* ---------------- 事件绑定 ---------------- */
function rvBind() {
  $("#btnRvStart").addEventListener("click", () => {
    const f = $("#rvStartForm");
    f.dataset.open = f.classList.contains("hidden") ? "1" : "";
    f.classList.toggle("hidden");
  });
  $("#rvMode").addEventListener("change", (e) => {
    const ratio = e.target.value === "ratio";
    $("#rvCount").disabled = ratio;
    $("#rvRatio").disabled = !ratio;
  });
  $("#rvFailLimit").addEventListener("input", (e) => { e.target.dataset.touched = "1"; });
  $("#btnRvSaveLimit").addEventListener("click", async () => {
    try {
      state.data = await post(`/api/reels/${state.reelId}/reviews/settings`,
        { fail_limit: +$("#rvDefaultLimit").value || 0 });
      renderAll(); toast("已保存默认失败门限");
    } catch (e) {}
  });
  $("#btnRvDoStart").addEventListener("click", rvStart);
  $("#btnRvClose").addEventListener("click", () => {
    $("#rvModal").classList.add("hidden");
    $("#btnRvPassRound").classList.remove("hidden");
    $("#btnRvExtend").classList.remove("hidden");
    $("#btnRvReturn").classList.remove("hidden");
    loadState();
  });
  $("#btnRvPrev").addEventListener("click", () => {
    rv.pos -= 1; rvRenderCurrent();
  });
  $("#btnRvNext").addEventListener("click", () => {
    rv.pos += 1; rvRenderCurrent();
  });
  $("#btnRvPass").addEventListener("click", () => rvJudge("pass"));
  $("#btnRvFail").addEventListener("click", () => rvJudge("fail"));
  document.querySelectorAll("#rvDims input[type=radio]").forEach((rb) =>
    rb.addEventListener("change", () => { if (rv.mode === "anon") rvSyncJudgeButtons(); }));
  $("#btnRvPassRound").addEventListener("click", () => rvFinish("pass"));
  $("#btnRvExtend").addEventListener("click", rvExtend);
  $("#btnRvReturn").addEventListener("click", () => rvFinish("return"));
  $("#btnRvHandoffJson").addEventListener("click", () => {
    if (state.reelId) location.href = `/api/reels/${state.reelId}/reviews/handoff.json`;
  });
  $("#btnRvHandoffCsv").addEventListener("click", () => {
    if (state.reelId) location.href = `/api/reels/${state.reelId}/reviews/handoff.csv`;
  });
}

rvBind();
window.rvRender = rvRender;
