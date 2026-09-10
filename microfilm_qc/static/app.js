/* 微缩胶片质检 —— 前端交互 */
"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  reelId: null,
  data: null,          // 服务端 state
  selected: null,      // 当前帧 id
  compare: [],         // 对比帧 id（最多 2）
  zoom: 140,
  dragId: null,
};

const WARN_LABEL = {
  missing: "缺帧", duplicate: "重复扫描", inversion: "顺序倒置",
  orientation: "方向异常", brightness: "亮度突变",
};

/* ---------------- API ---------------- */
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    toast(j.error || ("请求失败 " + r.status), true);
    if (j.state) { state.data = j.state; renderAll(); }
    throw new Error(j.error || r.status);
  }
  return j;
}
const post = (url, body) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function toast(msg, isErr = false) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

/* ---------------- 卷盘管理 ---------------- */
async function loadReels() {
  const reels = await api("/api/reels");
  const sel = $("#reelSelect");
  sel.innerHTML = "";
  if (!reels.length) {
    sel.innerHTML = "<option value=''>（无卷盘）</option>";
    $("#main").classList.add("hidden");
    $("#empty").classList.remove("hidden");
    return;
  }
  for (const r of reels) {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = `${r.reel_no} · ${r.name}（${r.frames}帧${r.finalized ? "，已定稿" : ""}）`;
    sel.appendChild(o);
  }
  if (!state.reelId || !reels.some((r) => r.id === state.reelId)) state.reelId = reels[0].id;
  sel.value = state.reelId;
  await loadState();
}

async function loadState() {
  if (!state.reelId) return;
  state.data = await api(`/api/reels/${state.reelId}/state`);
  $("#empty").classList.add("hidden");
  $("#main").classList.remove("hidden");
  renderAll();
}

/* ---------------- 渲染 ---------------- */
function frameById(id) { return state.data.frames.find((f) => f.id === id); }

function renderAll() {
  renderInfo();
  renderStrip();
  renderWarnings();
  renderDetail();
  renderChecks();
  $("#btnUndo").disabled = !state.data.can_undo;
}
window.renderAll = renderAll;

function renderInfo() {
  const d = state.data;
  const active = d.frames.filter((f) => !f.excluded);
  $("#reelInfo").textContent =
    `卷号 ${d.reel.reel_no} ｜ 共 ${active.length} 帧` +
    (d.reel.finalized ? " ｜ ✅ 已定稿" : " ｜ 未定稿");
}

function warnCountOf(fid) {
  return state.data.warnings.filter((w) => !w.resolved && w.frame_id === fid).length;
}

function renderStrip() {
  const strip = $("#strip");
  strip.innerHTML = "";
  const h = Math.round(state.zoom * 1.35);
  for (const f of state.data.frames) {
    const div = document.createElement("div");
    div.className = "frame" +
      (f.id === state.selected ? " selected" : "") +
      (state.compare.includes(f.id) ? " compared" : "") +
      (f.excluded ? " excluded" : "") +
      (f.placeholder ? " placeholder" : "") +
      (f.reshoot ? " reshoot" : "");
    div.dataset.fid = f.id;
    div.draggable = true;
    const img = document.createElement("img");
    img.src = `/api/frame/${f.id}/thumb?w=${state.zoom}&_=${f.rotation}`;
    img.width = state.zoom; img.height = h;
    img.alt = "No." + f.frame_no;
    div.appendChild(img);
    const no = document.createElement("div");
    no.className = "fno";
    no.textContent = "No." + f.frame_no + (f.rotation ? ` ⟳${f.rotation}°` : "");
    div.appendChild(no);
    if (f.version_kind === "fill" || f.version_kind === "reshoot") {
      const rb = document.createElement("span");
      rb.className = "rbmark";
      rb.title = f.source || "补扫回填";
      rb.textContent = "补";
      div.appendChild(rb);
    }
    if (warnCountOf(f.id)) {
      const dot = document.createElement("span");
      dot.className = "wdot";
      dot.title = "有未处理告警";
      div.appendChild(dot);
    }
    // 选择 / 对比
    div.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey) toggleCompare(f.id);
      else { state.selected = f.id; renderStrip(); renderDetail(); }
    });
    // 拖拽纠序
    div.addEventListener("dragstart", (e) => { state.dragId = f.id; e.dataTransfer.effectAllowed = "move"; });
    div.addEventListener("dragover", (e) => {
      e.preventDefault();
      document.querySelectorAll(".frame").forEach((x) => x.classList.remove("drop-before", "drop-after"));
      const rect = div.getBoundingClientRect();
      div.classList.add(e.clientX < rect.left + rect.width / 2 ? "drop-before" : "drop-after");
    });
    div.addEventListener("dragleave", () => div.classList.remove("drop-before", "drop-after"));
    div.addEventListener("drop", async (e) => {
      e.preventDefault();
      const before = div.classList.contains("drop-before");
      div.classList.remove("drop-before", "drop-after");
      if (state.dragId == null || state.dragId === f.id) return;
      const frames = state.data.frames;
      let to = frames.findIndex((x) => x.id === f.id) + (before ? 0 : 1);
      const from = frames.findIndex((x) => x.id === state.dragId);
      if (from < to) to -= 1;
      state.data = await post(`/api/reels/${state.reelId}/move`, { frame_id: state.dragId, to_index: to });
      state.dragId = null;
      renderAll();
      toast("已调整帧序，连续性已重算");
    });
    strip.appendChild(div);
  }
}

function renderWarnings() {
  const ul = $("#warnings");
  ul.innerHTML = "";
  const ws = state.data.warnings;
  $("#warnCount").textContent = ws.filter((w) => !w.resolved).length || "";
  if (!ws.length) {
    ul.innerHTML = "<li><span class='hint'>无告警</span></li>";
    return;
  }
  for (const w of ws) {
    const li = document.createElement("li");
    if (w.resolved) li.classList.add("resolved");
    const tag = document.createElement("span");
    tag.className = "wtype";
    tag.textContent = WARN_LABEL[w.type] || w.type;
    const msg = document.createElement("span");
    msg.className = "wmsg";
    msg.textContent = w.message;
    msg.style.cursor = "pointer";
    msg.title = "点击定位到相关帧";
    msg.addEventListener("click", () => {
      if (w.frame_id) {
        state.selected = w.frame_id;
        renderStrip(); renderDetail();
        const el = document.querySelector(`.frame[data-fid="${w.frame_id}"]`);
        if (el) el.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
      }
    });
    const btn = document.createElement("button");
    btn.textContent = w.resolved ? "恢复" : "确认";
    btn.addEventListener("click", async () => {
      state.data = await post(`/api/warning/${w.id}/resolve`, { value: !w.resolved });
      renderAll();
    });
    li.append(tag, msg, btn);
    ul.appendChild(li);
  }
}

function renderDetail() {
  const box = $("#detail");
  const f = state.selected != null ? frameById(state.selected) : null;
  if (!f) {
    box.innerHTML = "<p class='hint'>点击胶片带中的帧进行选择；Ctrl+点击可多选并排比较。</p>";
    $("#compare").classList.add("hidden");
    return;
  }
  const flags = [
    f.placeholder ? "缺帧占位" : null, f.excluded ? "已剔除" : null,
    f.reshoot ? "标记重拍" : null, f.rotation ? `旋转 ${f.rotation}°` : null,
  ].filter(Boolean).join(" ｜ ") || "正常";
  box.innerHTML = `
    <div class="detail-grid">
      <img src="/api/frame/${f.id}/preview?w=360&_=${f.rotation}" alt="">
      <div class="meta">
        <div><b>No.${f.frame_no}</b>　${f.filename || "（占位帧）"}</div>
        <div>状态：${flags}</div>
        <div>亮度：${f.brightness.toFixed(1)}　方向分：${f.orient_score.toFixed(0)}</div>
        <div>${f.source ? "当前有效图来源：" + f.source + (f.version_count > 1 ? `（共 ${f.version_count} 个历史版本）` : "") : ""}</div>
        <div>${f.note ? "备注：" + f.note : ""}</div>
      </div>
    </div>
    <div class="opbar">
      ${f.placeholder ? "" : `
        <button data-op="rot90">↻ 旋转90°</button>
        <button data-op="rot180">⟳ 旋转180°</button>
        <button data-op="exclude">${f.excluded ? "恢复（取消剔除）" : "剔除（重复/废帧）"}</button>`}
      <button data-op="reshoot">${f.reshoot ? "取消重拍标记" : "标记重拍"}</button>
      <button data-op="insBefore">前插缺帧占位</button>
      <button data-op="insAfter">后插缺帧占位</button>
      ${f.placeholder ? `<button data-op="del" class="danger">删除占位帧</button>` : ""}
      <button data-op="cmp">加入对比</button>
    </div>`;
  box.querySelectorAll("button[data-op]").forEach((b) =>
    b.addEventListener("click", () => frameOp(f, b.dataset.op)));
  renderCompare();
}

async function frameOp(f, op) {
  try {
    if (op === "rot90") state.data = await post(`/api/frame/${f.id}/rotate`, { deg: 90 });
    else if (op === "rot180") state.data = await post(`/api/frame/${f.id}/rotate`, { deg: 180 });
    else if (op === "exclude") state.data = await post(`/api/frame/${f.id}/exclude`, { value: !f.excluded });
    else if (op === "reshoot") state.data = await post(`/api/frame/${f.id}/reshoot`, { value: !f.reshoot });
    else if (op === "del") state.data = await api(`/api/frame/${f.id}`, { method: "DELETE" });
    else if (op === "insBefore" || op === "insAfter") {
      const no = prompt("输入占位帧号：", suggestMissingNo());
      if (no == null || no.trim() === "") return;
      const idx = state.data.frames.findIndex((x) => x.id === f.id) + (op === "insAfter" ? 1 : 0);
      state.data = await post(`/api/reels/${state.reelId}/placeholder`, { frame_no: no.trim(), index: idx });
    } else if (op === "cmp") { toggleCompare(f.id); return; }
    renderAll();
    toast("操作完成，连续性已重算（可撤销）");
  } catch (e) { /* toast 已提示 */ }
}

function suggestMissingNo() {
  const w = state.data.warnings.find((x) => !x.resolved && x.type === "missing");
  if (w) {
    const m = w.message.match(/缺少 (\d+)/);
    if (m) return m[1];
  }
  return "";
}

/* ---------------- 并排比较 ---------------- */
function toggleCompare(id) {
  const i = state.compare.indexOf(id);
  if (i >= 0) state.compare.splice(i, 1);
  else {
    state.compare.push(id);
    if (state.compare.length > 2) state.compare.shift();
  }
  renderStrip(); renderDetail();
}

function renderCompare() {
  const box = $("#compare");
  if (state.compare.length !== 2) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  const [a, b] = state.compare.map(frameById);
  if (!a || !b) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `
    <figure><img src="/api/frame/${a.id}/preview?w=520&_=${a.rotation}">
      <figcaption>No.${a.frame_no} ${a.filename || "占位"} ｜ 亮度 ${a.brightness.toFixed(1)}</figcaption></figure>
    <figure><img src="/api/frame/${b.id}/preview?w=520&_=${b.rotation}">
      <figcaption>No.${b.frame_no} ${b.filename || "占位"} ｜ 亮度 ${b.brightness.toFixed(1)}</figcaption></figure>
    <div class="compare-info">并排比较中：可分别对两帧执行旋转 / 剔除 / 重拍；拖拽胶片带可调整顺序</div>`;
}

/* ---------------- 定稿检查 ---------------- */
function renderChecks() {
  const panel = $("#checksPanel");
  const c = state.data.checks;
  if (!c) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const ul = $("#checksList");
  ul.innerHTML = "";
  for (const item of c.checks) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="${item.ok ? "ok" : "bad"}">${item.ok ? "✔" : "✘"}</span>
      <span class="label">${item.label}</span><span class="detail">${item.detail}</span>`;
    ul.appendChild(li);
  }
  const btn = $("#btnFinalize");
  btn.textContent = state.data.reel.finalized ? "已定稿 ✓" : (c.passed ? "通过检查并定稿" : "定稿（存在未通过项）");
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("#reelSelect").addEventListener("change", (e) => { state.reelId = +e.target.value; loadState(); });
  $("#zoom").addEventListener("input", (e) => { state.zoom = +e.target.value; renderStrip(); });

  $("#btnSample").addEventListener("click", async () => {
    toast("正在生成演示卷盘…");
    const j = await post("/api/sample");
    state.reelId = j.reel_id;
    await loadReels();
    toast("演示卷盘已载入：含缺帧/重复/倒置/旋转/亮度突变样例");
  });

  $("#btnImport").addEventListener("click", () => $("#importPanel").classList.toggle("hidden"));
  $("#btnDoImport").addEventListener("click", async () => {
    const z = $("#zipFile").files[0];
    if (!z) return toast("请选择帧图像 ZIP", true);
    const fd = new FormData();
    fd.append("zip", z);
    const m = $("#manifestFile").files[0];
    if (m) fd.append("manifest", m);
    if ($("#reelName").value) fd.append("name", $("#reelName").value);
    if ($("#reelNo").value) fd.append("reel_no", $("#reelNo").value);
    toast("导入中，正在计算感知指纹…");
    const j = await api("/api/import", { method: "POST", body: fd });
    state.reelId = j.reel_id;
    $("#importPanel").classList.add("hidden");
    await loadReels();
    toast("导入完成");
  });

  $("#btnDeleteReel").addEventListener("click", async () => {
    if (!state.reelId || !confirm("确定删除当前卷盘及其全部修订记录？")) return;
    await api(`/api/reels/${state.reelId}`, { method: "DELETE" });
    state.reelId = null; state.selected = null; state.compare = [];
    await loadReels();
  });

  $("#btnUndo").addEventListener("click", async () => {
    try {
      const j = await post(`/api/reels/${state.reelId}/undo`);
      state.data = j.state;
      renderAll();
      toast("已撤销：" + j.undone);
    } catch (e) {}
  });

  $("#btnFinalize").addEventListener("click", async () => {
    try {
      state.data = await post(`/api/reels/${state.reelId}/finalize`);
      renderAll();
      toast("定稿完成，可导出移交清单");
    } catch (e) {}
  });

  $("#btnExportJson").addEventListener("click", () => location.href = `/api/reels/${state.reelId}/export/manifest.json`);
  $("#btnExportCsv").addEventListener("click", () => location.href = `/api/reels/${state.reelId}/export/manifest.csv`);
  $("#btnExportReshoot").addEventListener("click", () => location.href = `/api/reels/${state.reelId}/export/reshoot.csv`);
  $("#btnExportContact").addEventListener("click", () => location.href = `/api/reels/${state.reelId}/export/contact.png`);
}

bind();
loadReels();
