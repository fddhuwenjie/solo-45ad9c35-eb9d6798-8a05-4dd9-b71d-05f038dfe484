/* 补扫回填模块：批次导入、并排预览、图像配准核对（叠加/闪烁/差异图/微调）、
   接受/拒绝/改绑、批量接受门禁与导出。
   依赖 app.js 暴露的全局 $ / api / post / toast / state / loadState。*/
"use strict";

const rb = {
  detail: null,
  filter: "all",
  reg: {},   // itemId -> {view, blend, blink, dx_full, dy_full, rotation, dirty, busy}
};

const REG_META = {
  ok:         {label: "核对一致", cls: "ok"},
  low:        {label: "相似度偏低·需人工核对", cls: "low"},
  failed:     {label: "配准失败", cls: "fail"},
  no_original:{label: "无原图可比", cls: "none"},
};

function rbRegState(it) {
  const r = it.reg;
  if (!r) return {label: "未配准", cls: "idle"};
  return REG_META[r.status] || {label: r.status_label || r.status, cls: "idle"};
}

function rbRegGated(it) {
  // 低于阈值/配准失败/无原图，以及未完成核对（空状态）都不可批量接受、单项需填理由
  const s = it.reg && it.reg.status;
  return s !== "ok";
}

function rbRegUI(id) {
  if (!rb.reg[id]) rb.reg[id] = {view: "overlay", blend: 0.5, blink: false, dx_full: 0, dy_full: 0, rotation: 0, dirty: false, busy: false};
  return rb.reg[id];
}

function rbEl() { return $("#rbItems"); }

function rbCountsText(d) {
  const c = d.counts;
  return `待处理 ${c.pending} ｜ 已拦截 ${c.blocked} ｜ 已接受 ${c.accepted} ｜ 已拒绝 ${c.rejected}（共 ${c.pending + c.blocked + c.accepted + c.rejected} 项）`;
}

/* ---------------- 批次下拉 / 与主界面同步 ---------------- */
function rbSyncBatches(batches, selectBatchId) {
  const bar = $("#rbBatchBar");
  const empty = $("#rescanEmpty");
  const sel = $("#rbBatchSelect");
  $("#rbBatchCount").textContent = batches.length || "";
  if (!batches.length) {
    bar.classList.add("hidden");
    empty.classList.remove("hidden");
    rb.detail = null;
    rbRender();
    return;
  }
  empty.classList.add("hidden");
  bar.classList.remove("hidden");
  const want = selectBatchId || (rb.detail && rb.detail.batch.id) || batches[0].id;
  sel.innerHTML = "";
  for (const b of batches) {
    const o = document.createElement("option");
    o.value = b.id;
    o.textContent = `#${b.id} ${b.name}（待${b.pending}/拦${b.blocked}/受${b.accepted}/拒${b.rejected}）`;
    sel.appendChild(o);
  }
  sel.value = batches.some((b) => b.id === want) ? want : batches[0].id;
  if (!rb.detail || rb.detail.batch.id !== +sel.value) rbLoad(+sel.value);
}

async function rbLoad(batchId) {
  rb.detail = await api(`/api/rescans/${batchId}`);
  rbRender();
}

/* 主界面刷新后同步批次列表（包一层 renderAll） */
const _origRenderAll = window.renderAll;
window.renderAll = function () {
  _origRenderAll.apply(this, arguments);
  if (state.data) {
    const keep = rb.detail && rb.detail.batch.id;
    rbSyncBatches(state.data.rescan_batches || [], keep);
  }
};

/* ---------------- 渲染条目 ---------------- */
function dims(o) { return o && o.width ? `${o.width}×${o.height}` : "—"; }
function br(o) { return o ? o.brightness.toFixed(0) : "—"; }
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function madCell(label, val, oldVal) {
  if (val == null && oldVal == null) return "";
  let cls = "";
  if (val != null && oldVal != null) cls = val <= oldVal + 3 ? "good" : "bad";
  else if (val != null) cls = val <= 25 ? "good" : "warn";
  return `<span class="${cls}">${label} MAD ${val == null ? "—" : val}${
    oldVal != null ? ` <i>（旧 ${oldVal}）</i>` : ""}</span>`;
}

function rbRender() {
  const box = rbEl();
  if (!box) return;
  if (!rb.detail) { box.innerHTML = ""; return; }
  const d = rb.detail;
  $("#rbBatchInfo").textContent = rbCountsText(d);
  const c = d.counts;
  const pendingItems = d.items.filter((it) => it.status === "pending");
  const autoOk = pendingItems.filter((it) => it.reg && it.reg.status === "ok").length;
  const needReview = pendingItems.filter(rbRegGated).length;
  const btn = $("#btnRbAcceptClean");
  btn.disabled = autoOk === 0;
  btn.textContent = autoOk
    ? `批量接受配准通过项（${autoOk}）`
    : "无配准通过项可批量接受";
  btn.title = needReview
    ? `另有 ${needReview} 个待处理项低于阈值/配准失败/无原图/尚未核对，需逐项人工确认`
    : "";
  let reviewNote = "";
  if (needReview) {
    const nLow = pendingItems.filter((it) => it.reg && it.reg.status === "low").length;
    const nFail = pendingItems.filter((it) => it.reg && it.reg.status === "failed").length;
    const nNone = pendingItems.filter((it) => it.reg && it.reg.status === "no_original").length;
    const nIdle = pendingItems.filter((it) => !(it.reg && it.reg.status)).length;
    const parts = [];
    if (nLow) parts.push(`相似度偏低 ${nLow}`);
    if (nFail) parts.push(`配准失败 ${nFail}`);
    if (nNone) parts.push(`无原图可比 ${nNone}`);
    if (nIdle) parts.push(`尚未核对 ${nIdle}`);
    reviewNote = ` ｜ ⚠ 不得批量接受：${parts.join("、")}`;
  }
  $("#rbRegSummary").textContent = reviewNote;
  $("#rbRegSummary").classList.toggle("warn-text", !!needReview);
  if (d.unused_files.length) {
    $("#rbUnused").textContent = "ZIP 中清单外文件：" + d.unused_files.join("、");
  } else {
    $("#rbUnused").textContent = "";
  }
  document.querySelectorAll(".rb-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.f === rb.filter));

  const items = d.items.filter((it) => rb.filter === "all" || it.status === rb.filter);
  if (rbBlinkTimer) { clearInterval(rbBlinkTimer); rbBlinkTimer = null; }
  box.innerHTML = "";
  for (const it of items) {
    const card = rbCard(it);
    box.appendChild(card);
    rbBindViewer(card, it);
    rbRenderViewer(card, it);
  }
}

function rbCard(it) {
  const card = document.createElement("div");
  card.className = "rb-card rb-" + it.status;
  const oldImg = it.target_frame_id
    ? `<img src="/api/frame/${it.target_frame_id}/preview?w=460" alt="">`
    : `<div class="rb-noimg">目标不存在</div>`;
  const newImg = it.file_id
    ? `<img src="/api/rescan-files/${it.file_id}/thumb?w=460" alt="">`
    : `<div class="rb-noimg">无补扫文件</div>`;
  const cont = it.continuity || {};
  card.innerHTML = `
    <div class="rb-card-head">
      <span class="rb-seq">#${it.seq + 1}</span>
      <span class="rb-target">原帧 No.${it.frame_no}</span>
      <span class="rb-file" title="${it.filename}">${it.filename || "（无文件名）"}</span>
      <span class="rb-status st-${it.status}">${it.status_label}</span>
    </div>
    <div class="rb-sides">
      <figure><figcaption>原帧 / 当前有效</figcaption>${oldImg}
        <div class="rb-meta">${rbMeta(it.old)}</div></figure>
      <figure><figcaption>补扫回填</figcaption>${newImg}
        <div class="rb-meta">${rbMeta(it.new)}</div></figure>
    </div>
    <div class="rb-cont">${rbCont(it)}</div>
    ${rbRegBlock(it)}
    ${it.block_reason ? `<div class="rb-reason">⛔ ${it.block_reason}</div>` : ""}
    ${it.decision_note ? `<div class="rb-note">备注：${it.decision_note}</div>` : ""}
    <div class="rb-ops"></div>`;
  const ops = card.querySelector(".rb-ops");
  const mk = (txt, cls, fn) => {
    const b = document.createElement("button");
    b.textContent = txt;
    if (cls) b.className = cls;
    b.addEventListener("click", fn);
    ops.appendChild(b);
  };

  if (it.status === "pending") {
    const gated = rbRegGated(it);
    if (gated) {
      mk("⚠ 强制接受（需填理由）", "danger", () => rbAccept(it.id, true));
    } else {
      mk("✔ 接受", "primary", () => rbAccept(it.id, false));
    }
    mk("✘ 拒绝", "danger", () => rbReject(it.id));
    mk("改绑到其他缺帧/重拍帧", "", () => rbRebind(it.id, it.frame_no));
  } else if (it.status === "blocked") {
    mk("改绑后接受", "primary", () => rbRebind(it.id, it.frame_no));
    mk("拒绝", "danger", () => rbReject(it.id));
  } else if (it.status === "accepted") {
    card.querySelector(".rb-sides").insertAdjacentHTML(
      "beforeend", `<div class="rb-done">已回填为当前有效图（可在顶部撤销）</div>`);
    if (it.reg && it.reg.force_reason) {
      card.insertAdjacentHTML("beforeend",
        `<div class="rb-force">⚠ 强制接受理由：${escapeHtml(it.reg.force_reason)}</div>`);
    }
  } else if (it.status === "rejected") {
    mk("改绑（恢复为待处理）", "", () => rbRebind(it.id, it.frame_no));
  }
  return card;
}

function rbMeta(o) {
  if (!o) return "—";
  return `尺寸 ${dims(o)} ｜ 方向 ${o.orient || "—"}（${(o.orient_score || 0).toFixed(0)}）｜ 亮度 ${br(o)}`;
}

function rbCont(it) {
  const c = it.continuity;
  if (!c || (!it.old || it.old.placeholder) && c.new_prev_mad == null) {
    return it.old && it.old.placeholder ? `<span class="hint">占位帧无原图指纹；接受后将以补扫图建立连续性基线。</span>` : "";
  }
  const parts = [];
  if (c.prev_no) parts.push(madCell(`与前帧 No.${c.prev_no}`, c.new_prev_mad, c.old_prev_mad));
  if (c.next_no) parts.push(madCell(`与后帧 No.${c.next_no}`, c.new_next_mad, c.old_next_mad));
  if (c.old_new_mad != null) parts.push(`<span>新旧图差异 MAD ${c.old_new_mad}</span>`);
  if (c.brightness_delta_prev != null) {
    const v = c.brightness_delta_prev;
    parts.push(`<span class="${Math.abs(v) >= 45 ? "bad" : "good"}">较前帧亮度差 ${v > 0 ? "+" : ""}${v}</span>`);
  }
  return parts.join(" ｜ ") || "";
}

/* ---------------- 图像配准核对 ---------------- */
function rbRegBlock(it) {
  const r = it.reg;
  if (!r) return "";
  const meta = rbRegState(it);
  const frozen = it.status === "accepted" || it.status === "rejected";
  const canCompare = !!it.target_frame_id && r.status !== "no_original";
  const badge = `<span class="rb-reg-badge reg-${r.status}">${meta.label}${r.manual ? "（人工微调）" : ""}</span>`;
  let body;
  if (r.status === "no_original") {
    body = `<div class="rb-reg-none">⚠ 目标为缺帧占位，没有原图可做配准比对。请直接查看补扫图与前后帧，
            确认内容一致后<b>强制接受必须填写理由</b>。</div>`;
  } else if (!canCompare) {
    body = `<div class="rb-reg-none hint">无法配准：${r.message || "缺少可比对的原帧"}。</div>`;
  } else {
    body = `
      <div class="rb-reg-stats">
        <span>最佳旋转 <b>${r.rotation}°</b></span>
        <span>平移 <b>dx ${r.dx_full ?? r.dx ?? 0} / dy ${r.dy_full ?? r.dy ?? 0}</b> px</span>
        <span class="${r.iou >= 0.72 ? "good" : "bad"}">结构相似度 <b>${(r.iou != null ? r.iou : 0).toFixed(3)}</b></span>
        <span class="${(r.edge_frac || 0) <= 0.12 ? "good" : "bad"}">未重合边缘 <b>${((r.edge_frac || 0) * 100).toFixed(1)}%</b></span>
        <span class="hint">亮度差 ${(r.lum_mad || 0).toFixed(0)}</span>
      </div>
      <div class="rb-reg-viewer" data-item="${it.id}">
        <div class="rb-reg-canvas"></div>
        <div class="rb-reg-controls">
          <div class="rb-reg-tabs">
            <button type="button" data-view="overlay">叠加</button>
            <button type="button" data-view="blink">闪烁</button>
            <button type="button" data-view="diff">差异图</button>
          </div>
          <label class="rb-slider">叠加程度
            <input type="range" class="rb-blend" min="0" max="100" value="${Math.round((rbRegUI(it.id).blend) * 100)}"></label>
          <label class="rb-slider">旋转
            <select class="rb-rot">${[0, 90, 180, 270].map((q) =>
              `<option value="${q}"${q === r.rotation ? " selected" : ""}>${q}°</option>`).join("")}</select></label>
          <div class="rb-nudge">
            <div class="rb-nudge-grid">
              <span></span><button type="button" data-nx="0" data-ny="-1">↑</button><span></span>
              <button type="button" data-nx="-1" data-ny="0">←</button><span class="rb-nudge-center">·</span><button type="button" data-nx="1" data-ny="0">→</button>
              <span></span><button type="button" data-nx="0" data-ny="1">↓</button><span></span>
            </div>
            <div class="rb-nudge-info">dx <b class="rb-dx">${r.dx_full ?? 0}</b> ｜ dy <b class="rb-dy">${r.dy_full ?? 0}</b></div>
            <button type="button" class="rb-recompute">应用微调并重算</button>
            <button type="button" class="rb-reset ghost">恢复自动结果</button>
            ${frozen ? '<div class="hint">条目已处理，核对记录已冻结（仅可查看）</div>' : ""}
          </div>
        </div>
      </div>`;
  }
  return `<div class="rb-reg">
      <div class="rb-reg-head">🧩 图像配准核对 ${badge}</div>
      ${body}
    </div>`;
}

function rbViewerURL(itemId, view, blend, ui) {
  const fmt = view === "diff" ? "diff" : view === "new" ? "new" : "overlay";
  let url = `/api/rescan-items/${itemId}/registration.${fmt}?w=620&_=${Date.now()}`;
  if (fmt === "overlay") url += `&blend=${blend}`;
  if (ui && ui.dirty) url += `&rotation=${ui.rotation}&dx_full=${ui.dx_full}&dy_full=${ui.dy_full}`;
  return url;
}

let rbBlinkTimer = null;

function rbRenderViewer(card, it) {
  const r = it.reg;
  if (!r || r.status === "no_original") return;
  const wrap = card.querySelector(".rb-reg-viewer");
  if (!wrap) return;
  const ui = rbRegUI(it.id);
  // 以当前详情数据初始化微调起点（首次或刚重算）
  if (!ui.dirty) { ui.rotation = r.rotation; ui.dx_full = r.dx_full ?? 0; ui.dy_full = r.dy_full ?? 0; }
  wrap.querySelectorAll(".rb-reg-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === ui.view));
  const canvas = wrap.querySelector(".rb-reg-canvas");
  const rotSel = wrap.querySelector(".rb-rot");
  if (rotSel) rotSel.value = String(ui.rotation);
  const dxEl = wrap.querySelector(".rb-dx"), dyEl = wrap.querySelector(".rb-dy");
  if (dxEl) dxEl.textContent = ui.dx_full;
  if (dyEl) dyEl.textContent = ui.dy_full;

  if (rbBlinkTimer) { clearInterval(rbBlinkTimer); rbBlinkTimer = null; }
  if (ui.view === "blink") {
    let showNew = false;
    const tick = () => {
      showNew = !showNew;
      // 闪烁：原图（blend=0）/对齐后补扫图（new）交替
      canvas.innerHTML = `<img src="${rbViewerURL(it.id, showNew ? "new" : "overlay", 0, ui)}" alt="">`;
    };
    tick();
    rbBlinkTimer = setInterval(tick, 650);
  } else {
    canvas.innerHTML = `<img src="${rbViewerURL(it.id, ui.view, ui.blend, ui)}" alt="">`;
  }
}

function rbBindViewer(card, it) {
  const wrap = card.querySelector(".rb-reg-viewer");
  if (!wrap) return;
  const ui = rbRegUI(it.id);
  const frozen = it.status === "accepted" || it.status === "rejected";
  wrap.querySelectorAll("button, input, select").forEach((el) => { el.disabled = frozen; });
  wrap.querySelectorAll(".rb-reg-tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      ui.view = b.dataset.view; rbRenderViewer(card, it);
    }));
  const blend = wrap.querySelector(".rb-blend");
  if (blend) blend.addEventListener("input", () => {
    ui.blend = (+blend.value) / 100; ui.view = "overlay";
    const img = wrap.querySelector(".rb-reg-canvas img");
    if (img) img.src = rbViewerURL(it.id, "overlay", ui.blend, ui);
  });
  const rot = wrap.querySelector(".rb-rot");
  if (rot) rot.addEventListener("change", () => {
    ui.rotation = +rot.value; ui.dirty = true;
    if (ui.view === "blink") rbRenderViewer(card, it);
    else {
      const img = wrap.querySelector(".rb-reg-canvas img");
      if (img) img.src = rbViewerURL(it.id, ui.view, ui.blend, ui);
    }
  });
  wrap.querySelectorAll("[data-nx]").forEach((btn) =>
    btn.addEventListener("click", () => {
      ui.dx_full += +btn.dataset.nx * 5; ui.dy_full += +btn.dataset.ny * 5; ui.dirty = true;
      const dxEl = wrap.querySelector(".rb-dx"), dyEl = wrap.querySelector(".rb-dy");
      dxEl.textContent = ui.dx_full; dyEl.textContent = ui.dy_full;
      if (ui.view === "blink") rbRenderViewer(card, it);
      else {
        const img = wrap.querySelector(".rb-reg-canvas img");
        if (img) img.src = rbViewerURL(it.id, ui.view, ui.blend, ui);
      }
    }));
  wrap.querySelector(".rb-recompute").addEventListener("click", () => rbRecompute(card, it));
  wrap.querySelector(".rb-reset").addEventListener("click", async () => {
    ui.dirty = false;
    await rbRecompute(card, it, false);
  });
}

async function rbRecompute(card, it, manual = true) {
  const ui = rbRegUI(it.id);
  if (ui.busy) return;
  ui.busy = true;
  const body = manual && ui.dirty
    ? {manual: true, rotation: ui.rotation, dx_full: ui.dx_full, dy_full: ui.dy_full}
    : {};
  try {
    const j = await post(`/api/rescan-items/${it.id}/registration`, body);
    rb.detail = j.detail;
    const fresh = rb.detail.items.find((x) => x.id === it.id);
    ui.dirty = false;
    // 替换卡片后必须重新绑定并渲染查看器，否则叠加/闪烁/差异图与微调控件失效
    const newCard = rbInternals.renderCard(fresh, card);
    rbInternals.bindViewer(newCard, fresh);
    rbInternals.renderViewer(newCard, fresh);
    toast("已按当前对齐重算配准指标");
  } catch (e) { /* toast 已提示 */ } finally { ui.busy = false; }
}

// 经一层可变引用调用，便于无浏览器环境下的回归测试注入桩件
const rbInternals = {
  renderCard: (it, oldCard) => rbRenderCard(it, oldCard),
  bindViewer: (card, it) => rbBindViewer(card, it),
  renderViewer: (card, it) => rbRenderViewer(card, it),
  rbCard: (it) => rbCard(it),
};
window.__rbTest = { rbInternals, rbRecompute, rbRegUI };

function rbRenderCard(it, oldCard) {
  const fresh = rbCard(it);
  oldCard.replaceWith(fresh);
  return fresh;
}

/* ---------------- 操作 ---------------- */
async function rbApply(r, okMsg) {
  if (r.status === 400) {
    const j = await r.json().catch(() => ({}));
    if (j.detail) { rb.detail = j.detail; state.data = j.state || state.data; rbRender(); }
    toast(j.error || "操作失败", true);
    return false;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || ("请求失败 " + r.status), true); return false; }
  if (j.detail) rb.detail = j.detail;
  if (j.state) { state.data = j.state; _origRenderAll(); }
  else if (state.reelId) await loadState();
  rbRender();
  toast(okMsg || "操作完成（可撤销）");
  return true;
}

async function rbAccept(id, gated) {
  let forceReason = "";
  if (gated) {
    forceReason = prompt("配准核对未通过（相似度偏低 / 配准失败 / 无原图可比）。\n" +
      "确属同一帧时，请填写强制接受理由（必填）：", "");
    if (forceReason === null) return;
    if (!forceReason.trim()) { toast("强制接受必须填写理由", true); return; }
  }
  const note = prompt("接受备注（可留空）：", "") || "";
  await rbApply(await fetch(`/api/rescan-items/${id}/accept`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note, force_reason: forceReason }),
  }), gated ? "已强制接受：理由与配准核对记录已随批次保存"
            : "已接受：原文件与来源关系已保留，仅重算该帧及相邻帧告警");
}

async function rbReject(id) {
  const note = prompt("拒绝原因 / 备注：", "");
  if (note === null) return;
  await rbApply(await fetch(`/api/rescan-items/${id}/reject`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  }), "已拒绝（可撤销 / 可改绑恢复）");
}

async function rbRebind(id, oldNo) {
  const no = prompt("改绑到本卷哪个帧号？（须为缺帧占位或重拍帧）", oldNo);
  if (no === null || !no.trim()) return;
  await rbApply(await fetch(`/api/rescan-items/${id}/rebind`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ frame_no: no.trim() }),
  }), "已改绑（可撤销）");
}

async function rbAcceptClean() {
  if (!rb.detail) return;
  if (!confirm("仅接受配准核对通过（结构相似度达标）的待处理项；\n" +
      "低于阈值、配准失败或无原图可比的条目会被跳过，需逐项人工核对。继续？")) return;
  const j = await post(`/api/rescans/${rb.detail.batch.id}/accept-clean`, {});
  rb.detail = j.detail;
  if (j.state) { state.data = j.state; _origRenderAll(); }
  rbRender();
  const nSkip = (j.skipped || []).length;
  const nErr = (j.errors || []).length;
  if (nSkip) {
    toast(`已批量接受通过项；${nSkip} 项需人工核对（低置信/失败/无原图），不可批量接受`, true);
  } else if (nErr) {
    toast(`完成，${nErr} 项未接受（见拦截原因）`, true);
  } else {
    toast("全部配准通过项已接受（可撤销）");
  }
}

/* ---------------- 绑定 ---------------- */
async function rbRefreshAfterStateChange() {
  // 撤销等全局操作可能回滚补扫条目状态，重新拉取当前批次
  const sel = $("#rbBatchSelect");
  if (sel && !sel.parentElement.classList.contains("hidden")) {
    await rbLoad(+sel.value);
  }
}

function rbBind() {
  $("#rbBatchSelect").addEventListener("change", (e) => rbLoad(+e.target.value));
  document.querySelectorAll(".rb-tab").forEach((t) =>
    t.addEventListener("click", () => { rb.filter = t.dataset.f; rbRender(); }));

  $("#btnRescanImport").addEventListener("click", () =>
    $("#rescanImportPanel").classList.toggle("hidden"));
  $("#btnDoRescanImport").addEventListener("click", async () => {
    const z = $("#rbZip").files[0];
    const mf = $("#rbManifest").files[0];
    if (!z || !mf) return toast("请同时选择补扫 ZIP 和回填清单", true);
    const fd = new FormData();
    fd.append("zip", z);
    fd.append("manifest", mf);
    if ($("#rbName").value) fd.append("name", $("#rbName").value);
    toast("导入并校验中…");
    const j = await api(`/api/reels/${state.reelId}/rescans/import`,
                        { method: "POST", body: fd });
    rb.detail = j.detail;
    state.data = j.state;
    _origRenderAll();
    rbRender();
    const c = j.detail.counts;
    toast(`导入完成：${c.pending} 项待处理，${c.blocked} 项被拦截`);
    $("#rescanImportPanel").classList.add("hidden");
  });

  $("#btnSampleRescan").addEventListener("click", async () => {
    const j = await post(`/api/reels/${state.reelId}/sample-rescan`, {});
    rb.detail = j.detail;
    state.data = j.state;
    _origRenderAll();
    rbRender();
    toast("演示补扫批次已生成：含改名回填与各类拦截样例");
  });

  $("#btnRbAcceptClean").addEventListener("click", rbAcceptClean);
  $("#btnRbExportJson").addEventListener("click", () =>
    location.href = `/api/rescans/${rb.detail.batch.id}/export/batch.json`);
  $("#btnRbExportCsv").addEventListener("click", () =>
    location.href = `/api/rescans/${rb.detail.batch.id}/export/decisions.csv`);
  $("#btnRbExportManifest").addEventListener("click", () =>
    location.href = `/api/reels/${state.reelId}/export/manifest.csv`);

  // 全局撤销可能回滚补扫条目：撤销后刷新批次详情
  $("#btnUndo").addEventListener("click", () => setTimeout(rbRefreshAfterStateChange, 60));
  // 切换卷盘后自动加载该卷首个补扫批次
  $("#reelSelect").addEventListener("change", () => {
    rb.detail = null;
    $("#rbBatchSelect").innerHTML = "";
  });
}

rbBind();
