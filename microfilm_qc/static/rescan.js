/* 补扫回填模块：批次导入、并排预览、接受/拒绝/改绑、批量接受与导出。
   依赖 app.js 暴露的全局 $ / api / post / toast / state / loadState。*/
"use strict";

const rb = {
  detail: null,
  filter: "all",
};

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
  $("#btnRbAcceptClean").disabled = c.pending === 0;
  if (d.unused_files.length) {
    $("#rbUnused").textContent = "ZIP 中清单外文件：" + d.unused_files.join("、");
  } else {
    $("#rbUnused").textContent = "";
  }
  document.querySelectorAll(".rb-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.f === rb.filter));

  const items = d.items.filter((it) => rb.filter === "all" || it.status === rb.filter);
  box.innerHTML = "";
  for (const it of items) box.appendChild(rbCard(it));
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
    mk("✔ 接受", "primary", () => rbAccept(it.id));
    mk("✘ 拒绝", "danger", () => rbReject(it.id));
    mk("改绑到其他缺帧/重拍帧", "", () => rbRebind(it.id, it.frame_no));
  } else if (it.status === "blocked") {
    mk("改绑后接受", "primary", () => rbRebind(it.id, it.frame_no));
    mk("拒绝", "danger", () => rbReject(it.id));
  } else if (it.status === "accepted") {
    card.querySelector(".rb-sides").insertAdjacentHTML(
      "beforeend", `<div class="rb-done">已回填为当前有效图（可在顶部撤销）</div>`);
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

async function rbAccept(id) {
  const note = prompt("接受备注（可留空）：", "");
  if (note === null) return;
  await rbApply(await fetch(`/api/rescan-items/${id}/accept`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  }), "已接受：原文件与来源关系已保留，仅重算该帧及相邻帧告警");
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
  if (!confirm("将接受全部无冲突的待处理项，拦截项保留。继续？")) return;
  const j = await post(`/api/rescans/${rb.detail.batch.id}/accept-clean`, {});
  rb.detail = j.detail;
  if (j.state) { state.data = j.state; _origRenderAll(); }
  rbRender();
  toast(j.errors && j.errors.length
    ? `完成，${j.errors.length} 项未接受（见拦截原因）`
    : "全部无冲突项已接受（可撤销）");
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
