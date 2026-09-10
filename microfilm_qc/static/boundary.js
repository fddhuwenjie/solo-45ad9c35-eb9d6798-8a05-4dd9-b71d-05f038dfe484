/* 帧边界复核模块：候选检测、可拖动切线编辑器、连续帧合并、即时预览、确认/撤销/导出。
   依赖 app.js 暴露的全局 $ / api / post / toast / state / loadState。*/
"use strict";

const bd = {
  candidates: null,      // {splits, merges}
  loaded: false,
  mergeMode: false,
  picked: [],            // 合并选择模式下选中的 frame id（按选择顺序）
  editor: null,          // 当前编辑器状态
  scanning: false,
};

const CONF_HIGH = 0.75;
const confCls = (c) => (c >= CONF_HIGH ? "high" : c >= 0.6 ? "mid" : "low");

/* ---------------- 候选加载 / 渲染 ---------------- */
async function bdScan(force) {
  if (!state.reelId) return;
  bd.scanning = true;
  try {
    bd.candidates = await api(`/api/reels/${state.reelId}/boundary/candidates`);
    bd.loaded = true;
  } catch (e) {
    bd.candidates = { splits: [], merges: [] };
  } finally {
    bd.scanning = false;
  }
  if (force !== false && window.renderAll) window.renderAll.call(null);
  bdRenderPanel();
}

function bdFrameMarks(fid) {
  if (!bd.candidates) return { split: null, merge: null };
  const split = bd.candidates.splits.find((c) => c.frame_id === fid);
  const merge = bd.candidates.merges.find((c) => c.frame_ids.includes(fid));
  return { split, merge };
}

function bdRenderPanel() {
  const panel = $("#boundaryPanel");
  if (!state.data) return;
  const nSplit = bd.candidates ? bd.candidates.splits.length : 0;
  const nMerge = bd.candidates ? bd.candidates.merges.length : 0;
  if (state.data.boundary_ops.length || (bd.loaded && (nSplit || nMerge))) {
    panel.classList.remove("hidden");
  }
  $("#bdCount").textContent = bd.loaded ? `拆 ${nSplit} ｜ 合 ${nMerge}` : "";
  $("#boundarySummary").textContent = bd.loaded
    ? (nSplit + nMerge
        ? `检测到 ${nSplit} 个粘连拆分候选、${nMerge} 个误切合并候选`
        : "未发现明确的粘连/误切候选，仍可在帧详情中手动发起拆分/合并")
    : "";

  const sBox = $("#bdSplits");
  const mBox = $("#bdMerges");
  sBox.innerHTML = "";
  mBox.innerHTML = "";
  if (bd.candidates) {
    bd.candidates.splits.forEach((c) => sBox.appendChild(bdSplitCard(c)));
    bd.candidates.merges.forEach((c) => mBox.appendChild(bdMergeCard(c)));
  }
  if (!sBox.children.length) sBox.innerHTML = "<div class='hint'>无候选</div>";
  if (!mBox.children.length) mBox.innerHTML = "<div class='hint'>无候选</div>";
  bdRenderHistory();
}

function bdReasonList(reasons) {
  if (!reasons || !reasons.length) return "<ul class='bd-reasons'><li>画幅比例信号（依据较弱，建议人工核对）</li></ul>";
  return "<ul class='bd-reasons'>" + reasons.map((r) => `<li>${r}</li>`).join("") + "</ul>";
}

function bdSplitCard(c) {
  const card = document.createElement("div");
  card.className = "bd-card c-" + (c.confidence >= CONF_HIGH ? "high" : "mid");
  card.innerHTML = `
    <div class="bd-card-top">
      <span class="bd-conf ${confCls(c.confidence)}">置信度 ${(c.confidence * 100) | 0}%</span>
      <span class="bd-title">No.${c.frame_no} 疑似两帧粘连（${c.width}×${c.height}）</span>
    </div>
    ${bdReasonList(c.reasons)}
    <div class="bd-card-ops">
      <button class="primary" data-op="open">打开切线编辑器</button>
      <button data-op="locate">胶片带定位</button>
    </div>`;
  card.querySelector('[data-op="open"]').addEventListener("click", () => bdOpenSplit(c.frame_id, c));
  card.querySelector('[data-op="locate"]').addEventListener("click", () => bdLocate(c.frame_id));
  return card;
}

function bdMergeCard(c) {
  const card = document.createElement("div");
  card.className = "bd-card c-" + (c.confidence >= CONF_HIGH ? "high" : "mid");
  card.innerHTML = `
    <div class="bd-card-top">
      <span class="bd-conf ${confCls(c.confidence)}">置信度 ${(c.confidence * 100) | 0}%</span>
      <span class="bd-title">No.${c.frame_nos.join(" + No.")} 疑似误切两半（${c.layout === "v" ? "上下" : "左右"}拼合）</span>
    </div>
    ${bdReasonList(c.reasons)}
    <div class="bd-card-ops">
      <button class="primary" data-op="open">打开合并预览</button>
      <button data-op="locate">胶片带定位</button>
    </div>`;
  card.querySelector('[data-op="open"]').addEventListener("click", () => bdOpenMerge(c.frame_ids, c));
  card.querySelector('[data-op="locate"]').addEventListener("click", () => bdLocate(c.frame_ids[0]));
  return card;
}

function bdLocate(fid) {
  state.selected = fid;
  window.renderAll.call(null);
  const el = document.querySelector(`.frame[data-fid="${fid}"]`);
  if (el) el.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
}

function bdRenderHistory() {
  const box = $("#bdHistory");
  box.innerHTML = "";
  const ops = (state.data.boundary_ops || []).slice().reverse();
  if (!ops.length) {
    box.innerHTML = "<div class='hint'>尚无已确认的边界修订。</div>";
    return;
  }
  for (const op of ops) {
    const row = document.createElement("div");
    row.className = "bd-hist-item";
    const when = new Date(op.created_at * 1000).toLocaleString("zh-CN", { hour12: false });
    let desc;
    if (op.kind === "split") {
      const inputs = (op.input || []).map((s) => "No." + s.frame_no).join("、");
      desc = `拆分 ${inputs}（${(op.cuts || []).length} 条切线 → ${(op.outputs || []).length} 段）`;
    } else {
      const inputs = (op.input || []).map((s) => "No." + s.frame_no).join(" + ");
      desc = `合并 ${inputs} → 1 帧（${op.layout === "v" ? "上下" : "左右"}拼合）`;
    }
    const renum = op.renumbered ? Object.keys(op.renumbered).length : 0;
    row.innerHTML = `<span class="tag">${op.kind === "split" ? "拆分" : "合并"} #${op.id}</span>
      <span>${desc}</span>
      ${op.reason ? `<span class="hint">依据：${op.reason}</span>` : ""}
      ${renum ? `<span class="hint">${renum} 帧重新编号</span>` : ""}
      <span class="hint">${when}</span>
      <span class="spacer"></span>
      <a data-op="compare">前后对照图</a>`;
    row.querySelector('[data-op="compare"]').addEventListener("click", () => {
      location.href = `/api/boundary-ops/${op.id}/comparison.png`;
    });
    box.appendChild(row);
  }
}

/* ---------------- 胶片带标记 / 合并模式 ---------------- */
function bdStripDecorate(frameDiv, f) {
  const marks = bdFrameMarks(f.id);
  if (marks.split) {
    const b = document.createElement("span");
    b.className = "bdmark";
    b.textContent = "粘";
    b.title = "疑似两帧粘连（置信度 " + ((marks.split.confidence * 100) | 0) + "%），点击边界面板处理";
    frameDiv.appendChild(b);
    frameDiv.classList.add("bdcand-glow");
  }
  if (marks.merge) {
    const b = document.createElement("span");
    b.className = "bdmerge";
    b.textContent = "切";
    b.title = "疑似画面中间误切（与相邻帧合并）";
    frameDiv.appendChild(b);
  }
  if (f.boundary) {
    frameDiv.classList.add("bd-derived");
    const t = f.boundary.kind === "crop"
      ? `来自边界拆分（第 ${(f.boundary.region_index || 0) + 1}/${f.boundary.region_count || "?"} 段）`
      : "来自边界合并";
    frameDiv.title = (frameDiv.title ? frameDiv.title + "\n" : "") + t;
  }
  if (bd.mergeMode && bd.picked.includes(f.id)) frameDiv.classList.add("bdpick");
}

function bdStripClick(f) {
  if (!bd.mergeMode) return false;
  const i = bd.picked.indexOf(f.id);
  if (i >= 0) bd.picked.splice(i, 1);
  else bd.picked.push(f.id);
  bdRefreshPickInfo();
  window.renderAll.call(null);
  return true;
}

function bdRefreshPickInfo() {
  const nos = bd.picked.map((id) => {
    const f = state.data.frames.find((x) => x.id === id);
    return f ? "No." + f.frame_no : "";
  }).filter(Boolean);
  $("#mergePick").textContent = bd.mergeMode
    ? (bd.picked.length ? "已选：" + nos.join("、") + "（须为连续帧）" : "点击胶片带中连续的两帧或多帧")
    : "";
}

function bdToggleMergeMode() {
  bd.mergeMode = !bd.mergeMode;
  bd.picked = [];
  const btn = $("#btnMergeMode");
  btn.classList.toggle("on", bd.mergeMode);
  btn.textContent = "合并选择模式：" + (bd.mergeMode ? "开" : "关");
  bdRefreshPickInfo();
  window.renderAll.call(null);
}

/* ---------------- 模态 ---------------- */
function bdCloseModal() {
  $("#bdModal").classList.add("hidden");
  bd.editor = null;
}

function bdModal(title, bodyHtml, footHtml) {
  $("#bdModalTitle").textContent = title;
  $("#bdModalBody").innerHTML = bodyHtml;
  $("#bdModalFoot").innerHTML = footHtml;
  $("#bdModal").classList.remove("hidden");
}

/* ---------------- 拆分编辑器 ---------------- */
function bdOpenSplit(frameId, candidate) {
  const f = state.data.frames.find((x) => x.id === frameId);
  if (!f || f.placeholder) { toast("占位帧无法拆分", true); return; }
  const cuts = candidate && candidate.cuts
    ? candidate.cuts.map((c) => ({ axis: c.axis, pos: c.pos }))
    : [];
  bd.editor = { kind: "split", frame: f, candidate: candidate || null,
                cuts, drag: null, previewUrl: null };
  bdModal(`帧边界拆分 ｜ No.${f.frame_no}（${f.filename}）`,
          `<div class="bd-help">在图上<b>点击</b>添加切线；拖动红色切线或顶部手柄可微调；在右侧列表可输入精确像素值或删除切点。
           零宽片段、交叉切线将被拦截。确认后第 1 段就地保留，其余段优先填充其后的缺帧占位，再新增帧并对受影响数字区间重新编号。</div>
           <div class="bd-editor">
             <div class="bd-canvas-wrap"><canvas id="bdCanvas"></canvas></div>
             <div class="bd-side" id="bdSide"></div>
           </div>`,
          `<span id="bdError" class="bd-error"></span>
           <span class="spacer"></span>
           <label class="hint">判断依据备注
             <input type="text" class="reason-input" id="bdReason" style="width:260px"
               value="${candidate ? candidate.reasons[0].replace(/"/g, "&quot;") : ""}"></label>
           <button id="btnBdCancel">取消</button>
           <button id="btnBdOk" class="primary">确认拆分并生成帧</button>`);
  $("#btnBdCancel").addEventListener("click", bdCloseModal);
  $("#btnBdClose").onclick = bdCloseModal;
  $("#btnBdOk").addEventListener("click", bdConfirmSplit);
  bdInitCanvas(f, cuts);
  bdRenderSide();
  bdSchedulePreview();
}

function bdInitCanvas(f, cuts) {
  const img = new Image();
  const wrap = $(".bd-canvas-wrap");
  img.onload = () => {
    const ed = bd.editor;
    const maxW = Math.min(wrap.clientWidth - 20 || 900, 1000);
    const scale = Math.min(1, maxW / img.naturalWidth);
    ed.img = img;
    ed.natW = img.naturalWidth;
    ed.natH = img.naturalHeight;
    ed.scale = scale;
    ed.dispW = Math.round(img.naturalWidth * scale);
    ed.dispH = Math.round(img.naturalHeight * scale);
    const cv = $("#bdCanvas");
    cv.width = ed.dispW;
    cv.height = ed.dispH;
    bdDrawCanvas();
    cv.onmousedown = (e) => bdCanvasDown(e, cv);
    window.addEventListener("mousemove", bdCanvasMove);
    window.addEventListener("mouseup", bdCanvasUp);
  };
  img.src = `/api/frame/${f.id}/preview?w=1200&_=${f.rotation || 0}`;
}

function bdDrawCanvas() {
  const ed = bd.editor;
  if (!ed || !ed.img) return;
  const cv = $("#bdCanvas");
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(ed.img, 0, 0, cv.width, cv.height);
  ed.cuts.forEach((c, i) => {
    const vertical = c.axis === "x";
    const p = c.pos * ed.scale;
    ctx.strokeStyle = "rgba(224,72,60,.92)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(vertical ? p : 0, vertical ? 0 : p);
    ctx.lineTo(vertical ? p : cv.width, vertical ? cv.height : p);
    ctx.stroke();
    // 手柄
    ctx.fillStyle = "#e0483c";
    if (vertical) {
      ctx.fillRect(p - 6, 0, 12, 20);
      ctx.fillRect(p - 6, cv.height - 20, 12, 20);
    } else {
      ctx.fillRect(0, p - 6, 20, 12);
      ctx.fillRect(cv.width - 20, p - 6, 20, 12);
    }
    ctx.fillStyle = "#fff";
    ctx.font = "bold 12px sans-serif";
    ctx.fillText(String(i + 1), vertical ? p - 3 : 5, vertical ? 14 : p + 4);
  });
}

function bdEventPos(e, cv) {
  const r = cv.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function bdCanvasDown(e, cv) {
  const ed = bd.editor;
  const { x, y } = bdEventPos(e, cv);
  // 先检查是否点中已有切线（手柄/线体 8px）
  for (let i = 0; i < ed.cuts.length; i++) {
    const c = ed.cuts[i];
    const p = c.pos * ed.scale;
    if ((c.axis === "x" && Math.abs(x - p) <= 8) ||
        (c.axis === "y" && Math.abs(y - p) <= 8)) {
      ed.drag = i;
      return;
    }
  }
  // 新增切点：根据候选/画幅自动选择轴——宽图竖切(x)，高图横切(y)
  const autoAxis = ed.natW >= ed.natH ? "x" : "y";
  const pos = Math.round((autoAxis === "x" ? x : y) / ed.scale);
  if (pos > 20 && pos < (autoAxis === "x" ? ed.natW : ed.natH) - 20) {
    ed.cuts.push({ axis: autoAxis, pos });
    ed.cuts.sort((a, b) => (a.axis === b.axis ? a.pos - b.pos : 0));
    ed.drag = ed.cuts.findIndex((c) => c === ed.cuts[ed.cuts.length - 1]);
    bdDrawCanvas();
    bdRenderSide();
    bdSchedulePreview();
  }
}

function bdCanvasMove(e) {
  const ed = bd.editor;
  if (!ed || ed.drag == null) return;
  const cv = $("#bdCanvas");
  if (!cv) return;
  const { x, y } = bdEventPos(e, cv);
  const c = ed.cuts[ed.drag];
  const full = c.axis === "x" ? ed.natW : ed.natH;
  c.pos = Math.max(1, Math.min(full - 1, Math.round((c.axis === "x" ? x : y) / ed.scale)));
  bdDrawCanvas();
  bdRenderSide(true);
  bdSchedulePreview();
}

function bdCanvasUp() {
  const ed = bd.editor;
  if (ed && ed.drag != null) {
    ed.cuts.sort((a, b) => (a.axis === b.axis ? a.pos - b.pos : 0));
    ed.drag = null;
    bdRenderSide();
  }
}

let bdPreviewTimer = null;
function bdSchedulePreview() {
  clearTimeout(bdPreviewTimer);
  bdPreviewTimer = setTimeout(bdRenderPreview, 140);
}

async function bdRenderPreview() {
  const ed = bd.editor;
  if (!ed) return;
  const side = $("#bdSide");
  if (!side) return;
  let imgBox = side.querySelector(".bd-seg-preview");
  if (!imgBox) {
    imgBox = document.createElement("div");
    imgBox.className = "bd-seg-preview";
    side.appendChild(imgBox);
  }
  if (!ed.cuts.length) { imgBox.innerHTML = "<span class='hint'>暂无切线，在图上点击添加。</span>"; return; }
  const q = ed.cuts.map((c) => `cut=${c.axis}:${c.pos}`).join("&");
  const url = `/api/frame/${ed.frame.id}/boundary/preview?${q}&w=640&_=${Date.now()}`;
  imgBox.innerHTML = "<span class='hint'>生成预览…</span>";
  try {
    const r = await fetch(url);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      imgBox.innerHTML = `<span class="bd-error">${j.error || ("预览失败 " + r.status)}</span>`;
      return;
    }
    const blob = await r.blob();
    imgBox.innerHTML = "";
    const im = document.createElement("img");
    im.src = URL.createObjectURL(blob);
    im.title = "确认前的片段预览（红线为切线）";
    imgBox.appendChild(im);
  } catch (e) { /* 网络错误忽略 */ }
}

function bdRenderSide(liveDrag) {
  const ed = bd.editor;
  if (!ed) return;
  const side = $("#bdSide");
  if (!side) return;
  const c = ed.candidate;
  let html = "";
  if (c) {
    html += `<h5>系统判断（置信度 ${(c.confidence * 100) | 0}%）</h5><ul>` +
            c.reasons.map((r) => `<li>${r}</li>`).join("") + "</ul>";
  }
  html += `<h5>切线（${ed.cuts.length}）</h5><div id="bdCutRows"></div>`;
  html += `<div style="margin-top:6px"><button id="btnBdAddCut">＋ 在中点添加切线</button></div>`;
  html += `<h5>原图尺寸</h5><div>${ed.natW || "?"}×${ed.natH || "?"}px；${ed.natW >= ed.natH ? "宽图默认竖切（左右分开）" : "高图默认横切（上下分开）"}</div>`;
  html += `<h5>即时片段预览</h5>`;
  const oldPreview = side.querySelector(".bd-seg-preview");
  side.innerHTML = html;
  if (oldPreview) side.appendChild(oldPreview);
  else {
    const div = document.createElement("div");
    div.className = "bd-seg-preview";
    side.appendChild(div);
  }

  const rows = $("#bdCutRows");
  ed.cuts.forEach((cut, i) => {
    const full = cut.axis === "x" ? ed.natW : ed.natH;
    const row = document.createElement("div");
    row.className = "bd-cut-row";
    row.innerHTML = `<span>#${i + 1}</span><span>${cut.axis === "x" ? "竖切 x" : "横切 y"}</span>
      <input type="number" min="1" max="${full - 1}" value="${cut.pos}">
      <span class="hint">/ ${full}（${full ? ((cut.pos / full) * 100).toFixed(1) : 0}%）</span>
      <span class="spacer"></span><span class="x" title="删除切点">✕</span>`;
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      const v = parseInt(input.value, 10);
      if (Number.isFinite(v)) { cut.pos = Math.max(1, Math.min(full - 1, v)); }
      ed.cuts.sort((a, b) => (a.axis === b.axis ? a.pos - b.pos : 0));
      bdDrawCanvas(); bdRenderSide(); bdSchedulePreview();
    });
    row.querySelector(".x").addEventListener("click", () => {
      ed.cuts.splice(i, 1);
      bdDrawCanvas(); bdRenderSide(); bdSchedulePreview();
    });
    rows.appendChild(row);
  });
  $("#btnBdAddCut").addEventListener("click", () => {
    const axis = ed.natW >= ed.natH ? "x" : "y";
    const full = axis === "x" ? ed.natW : ed.natH;
    // 找一个最大的片段中点
    const bounds = [0, ...ed.cuts.filter((x) => x.axis === axis).map((x) => x.pos).sort((a, b) => a - b), full];
    let best = [0, 1];
    for (let i = 0; i < bounds.length - 1; i++) {
      if (bounds[i + 1] - bounds[i] > best[1] - best[0]) best = [bounds[i], bounds[i + 1]];
    }
    ed.cuts.push({ axis, pos: Math.round((best[0] + best[1]) / 2) });
    ed.cuts.sort((a, b) => (a.axis === b.axis ? a.pos - b.pos : 0));
    bdDrawCanvas(); bdRenderSide(); bdSchedulePreview();
  });
  if (liveDrag) {
    // 拖动过程中只同步输入框数值，避免整侧重建导致事件抖动
    const inputs = rows.querySelectorAll("input");
    ed.cuts.forEach((cut, i) => {
      if (inputs[i]) inputs[i].value = cut.pos;
    });
  }
}

async function bdConfirmSplit() {
  const ed = bd.editor;
  if (!ed) return;
  if (!ed.cuts.length) { toast("请至少添加一条切线", true); return; }
  const btn = $("#btnBdOk");
  btn.disabled = true;
  try {
    const j = await post(`/api/frame/${ed.frame.id}/boundary/split`, {
      cuts: ed.cuts,
      reason: $("#bdReason").value.trim(),
    });
    state.data = j.state;
    bdCloseModal();
    window.renderAll.call(null);
    await bdScan();
    toast(`拆分完成：生成 ${j.outputs.length} 帧，已重新编号并重算邻近告警（可撤销）`);
  } catch (e) {
    const err = $("#bdError");
    if (err) err.textContent = e.message || "操作被拦截";
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 合并编辑器 ---------------- */
function bdOpenMerge(frameIds, candidate) {
  const frames = frameIds.map((id) => state.data.frames.find((f) => f.id === id)).filter(Boolean);
  if (frames.length < 2) { toast("请选择至少两帧", true); return; }
  if (frames.some((f) => f.placeholder)) { toast("缺帧占位不能参与合并", true); return; }
  bd.editor = { kind: "merge", frames, candidate: candidate || null,
                layout: candidate ? candidate.layout : null };
  const title = "帧边界合并 ｜ " + frames.map((f) => "No." + f.frame_no).join(" + ");
  bdModal(title,
    `<div class="bd-help">将<b>连续</b>的误切帧按接缝拼回一页。非相邻帧、占位帧、同一物理文件重复占用、接合边尺寸差异过大会被拦截。
     确认后保留第一帧为合并结果，其余帧移除，受影响数字区间自动重新编号。</div>
     <div id="bdMergeErr" class="bd-error"></div>
     <div class="bd-merge-preview" id="bdMergePreview" style="margin-top:8px"><span class="hint">正在生成合并预览…</span></div>`,
    `<label class="hint">拼合方向
       <select id="bdLayout">
         <option value="">自动判断</option>
         <option value="v">上下拼合（横切线）</option>
         <option value="h">左右拼合（竖切线）</option>
       </select></label>
     <span class="spacer"></span>
     <label class="hint">判断依据备注
       <input type="text" class="reason-input" id="bdReason" style="width:240px"
         value="${candidate ? candidate.reasons[0].replace(/"/g, "&quot;") : ""}"></label>
     <button id="btnBdCancel">取消</button>
     <button id="btnBdOk" class="primary">确认合并</button>`);
  if (bd.editor.layout) $("#bdLayout").value = bd.editor.layout;
  $("#btnBdCancel").addEventListener("click", bdCloseModal);
  $("#btnBdClose").onclick = bdCloseModal;
  $("#bdLayout").addEventListener("change", (e) => { bd.editor.layout = e.target.value || null; bdMergePreview(); });
  $("#btnBdOk").addEventListener("click", bdConfirmMerge);
  bdMergePreview();
}

let bdMergeTimer = null;
function bdMergePreview() {
  const ed = bd.editor;
  if (!ed) return;
  clearTimeout(bdMergeTimer);
  bdMergeTimer = setTimeout(async () => {
    const box = $("#bdMergePreview");
    if (!box) return;
    const ids = ed.frames.map((f) => f.id).join(",");
    const url = `/api/reels/${state.reelId}/boundary/merge-preview?ids=${ids}&w=900&_=${Date.now()}`;
    box.innerHTML = "<span class='hint'>生成预览…</span>";
    const r = await fetch(url);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      box.innerHTML = "";
      const err = $("#bdMergeErr");
      if (err) err.textContent = j.error || ("预览失败 " + r.status);
      return;
    }
    $("#bdMergeErr").textContent = "";
    const blob = await r.blob();
    box.innerHTML = "";
    const im = document.createElement("img");
    im.src = URL.createObjectURL(blob);
    box.appendChild(im);
  }, 120);
}

async function bdConfirmMerge() {
  const ed = bd.editor;
  if (!ed) return;
  const btn = $("#btnBdOk");
  btn.disabled = true;
  try {
    const j = await post(`/api/reels/${state.reelId}/boundary/merge`, {
      frame_ids: ed.frames.map((f) => f.id),
      layout: $("#bdLayout").value || null,
      reason: $("#bdReason").value.trim(),
    });
    state.data = j.state;
    bd.picked = [];
    bdCloseModal();
    window.renderAll.call(null);
    await bdScan();
    toast("合并完成：已生成拼合帧并重新编号，邻近告警已重算（可撤销）");
  } catch (e) {
    const err = $("#bdMergeErr");
    if (err) err.textContent = e.message || "操作被拦截";
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 与 app.js 挂钩 ---------------- */
const _bdRenderAll = window.renderAll;
window.renderAll = function () {
  _bdRenderAll.apply(this, arguments);
  if (state.data) {
    bdRenderPanel();
    // 重渲染后合并选择信息保持
    bdRefreshPickInfo();
  }
};

// 胶片带：叠加候选标记（包装原 renderStrip 内建帧的创建——用 MutationObserver 太重，
// 直接在 renderAll 后给每个 .frame 补标记）。
const _bdPostStrip = window.renderAll;
window.renderAll = function () {
  _bdPostStrip.apply(this, arguments);
  if (!state.data) return;
  document.querySelectorAll("#strip .frame").forEach((el) => {
    if (el.dataset.bdDecor) return;
    const fid = parseInt(el.dataset.fid, 10);
    const f = state.data.frames.find((x) => x.id === fid);
    if (f) bdStripDecorate(el, f);
  });
};

// 帧详情中增加“手动拆分 / 加入合并”按钮：监听详情区点击（事件委托）
document.addEventListener("click", (e) => {
  const op = e.target && e.target.dataset && e.target.dataset.bdop;
  if (!op || !state.selected) return;
  const f = frameById(state.selected);
  if (!f) return;
  if (op === "manualSplit") {
    if (f.placeholder) return toast("占位帧无法拆分", true);
    bdOpenSplit(f.id, bdFrameMarks(f.id).split);
  } else if (op === "mergePick") {
    bd.mergeMode = true;
    bd.picked = [f.id];
    $("#btnMergeMode").classList.add("on");
    $("#btnMergeMode").textContent = "合并选择模式：开";
    bdRefreshPickInfo();
    window.renderAll.call(null);
    toast("再点击相邻的连续帧，然后使用合并预览");
  }
});

function bdInjectDetailButtons() {
  // 在帧详情 opbar 末尾注入两个按钮
  const box = $("#detail");
  const bar = box && box.querySelector(".opbar");
  if (!bar || bar.querySelector('[data-bdop]')) return;
  if (!state.selected) return;
  const f = frameById(state.selected);
  if (!f) return;
  const b1 = document.createElement("button");
  b1.dataset.bdop = "manualSplit";
  b1.textContent = "✂ 边界拆分";
  if (f.placeholder) b1.disabled = true;
  const b2 = document.createElement("button");
  b2.dataset.bdop = "mergePick";
  b2.textContent = "⛓ 加入合并选择";
  bar.append(b1, b2);
}

// 包装 renderDetail：app.js 渲染详情后注入按钮
setInterval(() => {
  if (state.data && state.selected != null) bdInjectDetailButtons();
}, 250);

/* ---------------- 绑定 ---------------- */
function bdBind() {
  $("#btnBoundaryScan").addEventListener("click", async () => {
    $("#boundarySummary").textContent = "正在分析列亮度谷 / 内容投影 / 帧号连续性…";
    await bdScan();
    const c = bd.candidates;
    toast(c.splits.length + c.merges.length
      ? `检测完成：${c.splits.length} 个拆分候选、${c.merges.length} 个合并候选`
      : "检测完成：未发现明确候选");
  });
  $("#btnMergeMode").addEventListener("click", bdToggleMerge);
  $("#btnBdExportJson").addEventListener("click", () => {
    if (state.reelId) location.href = `/api/reels/${state.reelId}/boundary/export/changes.json`;
  });
  $("#btnBdHelp").addEventListener("click", () => {
    const h = document.querySelector(".bd-modal-body .bd-help");
    if (h) h.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#bdModal").classList.contains("hidden")) bdCloseModal();
  });

  // 切换卷盘后重置候选
  $("#reelSelect").addEventListener("change", () => {
    bd.candidates = null; bd.loaded = false; bd.picked = []; bd.mergeMode = false;
    $("#btnMergeMode").classList.remove("on");
    $("#btnMergeMode").textContent = "合并选择模式：关";
  });
  // 撤销后刷新候选
  $("#btnUndo").addEventListener("click", () => setTimeout(() => bd.candidates && bdScan(false), 80));
}

// 拦截胶片带点击用于合并模式：包装 frame div 上的 click 不方便，
// 在 strip 上用捕获阶段监听。
document.addEventListener("click", (e) => {
  if (!bd.mergeMode) return;
  const el = e.target.closest("#strip .frame");
  if (!el) return;
  e.preventDefault();
  e.stopPropagation();
  const fid = parseInt(el.dataset.fid, 10);
  const f = state.data.frames.find((x) => x.id === fid);
  if (f) bdStripClick(f);
}, true);

// 在合并选择模式下双击候选确认条打开合并编辑器（按钮也在面板中）：
// 当 picked >= 2 时，显示浮动“合并预览”按钮
const _bdPickInfo = setInterval(() => {
  if (!bd.mergeMode) return;
  let btn = $("#btnOpenMergePicked");
  if (bd.picked.length >= 2) {
    if (!btn) {
      btn = document.createElement("button");
      btn.id = "btnOpenMergePicked";
      btn.className = "primary";
      btn.textContent = "打开合并预览（" + bd.picked.length + " 帧）";
      btn.style.marginLeft = "8px";
      btn.addEventListener("click", () => {
        // 按胶片带位置排序后打开
        const ordered = bd.picked
          .map((id) => state.data.frames.find((f) => f.id === id))
          .filter(Boolean)
          .sort((a, b) => a.position - b.position)
          .map((f) => f.id);
        const cand = bd.candidates && bd.candidates.merges.find(
          (c) => c.frame_ids.length === ordered.length
                && c.frame_ids.every((x) => ordered.includes(x)));
        bdOpenMerge(ordered, cand);
      });
      $("#mergePick").appendChild(btn);
    }
  } else if (btn) {
    btn.remove();
  }
}, 200);

bdBind();
