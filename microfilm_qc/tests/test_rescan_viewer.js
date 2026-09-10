/* rescan.js 查看器重绑回归测试（不依赖浏览器/DOM 库）。
 *
 * 覆盖缺陷：rbRecompute 用新卡片替换旧卡片后，必须重新绑定并渲染查看器，
 * 否则叠加/闪烁/差异图与微调控件全部失效（旧节点的监听器随节点被丢弃）。
 *
 * 运行：node tests/test_rescan_viewer.js（需要 node）。
 */
"use strict";
const fs = require("fs");
const path = require("path");

let failures = 0;
function assert(cond, msg) {
  if (!cond) { failures++; console.error("FAIL: " + msg); }
  else console.log("ok - " + msg);
}
function eq(a, b, msg) { assert(a === b, msg + ` (got ${JSON.stringify(a)}, want ${JSON.stringify(b)})`); }

/* ---------------- 极简 DOM stub ---------------- */
let postCalls = 0;
class FakeNode {
  constructor(tag) { this.tagName = (tag || "div").toUpperCase(); this.children = [];
    this.parent = null; this._listeners = {}; this._html = ""; this.attrs = {};
    this.disabled = false; this.value = ""; this.textContent = ""; this.dataset = {};
    this.classList = mkClassList(this); }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  replaceWith(c) { if (this.parent) {
      const i = this.parent.children.indexOf(this);
      this.parent.children[i] = c; c.parent = this.parent; } return c; }
  insertAdjacentHTML(_pos, html) { this._html += html; }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  dispatch(t, ev) { for (const fn of (this._listeners[t] || [])) fn(ev || {preventDefault(){}}); }
  click() { this.dispatch("click"); }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  querySelector(sel) { return FakeDoc.find(this, sel, false); }
  querySelectorAll(sel) { return FakeDoc.find(this, sel, true); }
}
function mkClassList(el) {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, force) => { const on = force === undefined ? !set.has(c) : !!force;
      on ? set.add(c) : set.delete(c); return on; },
    contains: (c) => set.has(c),
  };
}
const FakeDoc = {
  store: {},
  find(root, sel, all) {
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if (FakeDoc.matches(c, sel)) out.push(c);
        walk(c);
      }
    };
    walk(root);
    return all ? out : (out[0] || null);
  },
  matches(_el, _sel) { return false; },  // 用带选择器标记的专用元素代替
};

/* 提供带选择器键的元素，rbBindViewer 通过 querySelector 取得同一实例并可检查监听 */
function keyed(tag, key) { const e = new FakeNode(tag); e._key = key; return e; }

function buildCard() {
  const card = new FakeNode("div");
  // 结构与 rbRegBlock 中的选择器对应
  const viewer = keyed("div", ".rb-reg-viewer");
  const tabs = keyed("div", ".rb-reg-tabs");
  ["overlay", "blink", "diff"].forEach((v) => {
    const b = keyed("button", `tab-${v}`); b.dataset.view = v; tabs.appendChild(b);
  });
  viewer.appendChild(tabs);
  const blend = keyed("input", ".rb-blend"); blend.value = "50"; viewer.appendChild(blend);
  const rot = keyed("select", ".rb-rot"); rot.value = "0"; viewer.appendChild(rot);
  const nud = keyed("button", "nudge-up"); nud.dataset.nx = "0"; nud.dataset.ny = "-1"; viewer.appendChild(nud);
  const recompute = keyed("button", ".rb-recompute"); viewer.appendChild(recompute);
  const reset = keyed("button", ".rb-reset"); viewer.appendChild(reset);
  const canvas = keyed("div", ".rb-reg-canvas"); viewer.appendChild(canvas);
  const dx = keyed("b", ".rb-dx"); dx.textContent = "0"; viewer.appendChild(dx);
  const dy = keyed("b", ".rb-dy"); dy.textContent = "0"; viewer.appendChild(dy);
  card.appendChild(viewer);
  // 让 querySelector/All 按 _key 匹配
  card.querySelector = (s) => card._byKey(s) || viewer.querySelector(s);
  card.querySelectorAll = (s) => {
    if (s === "button, input, select" || s === "[data-nx]") {
      return card._collect().filter((c) => c.tagName === "BUTTON" || c.tagName === "INPUT" || c.tagName === "SELECT");
    }
    if (s === ".rb-reg-tabs button") return tabs.children;
    return card._collect().filter((c) => c._key === s);
  };
  card._byKey = (s) => card._collect().find((c) => c._key === s) || null;
  card._collect = function () { const a = []; (function w(n) { for (const c of n.children) { a.push(c); w(c); } })(card); return a; };
  viewer.querySelector = (s) => card._byKey(s);
  viewer.querySelectorAll = (s) => {
    if (s === ".rb-reg-tabs button") return tabs.children;
    if (s === "button, input, select") {
      return card._collect().filter((c) => c.tagName === "BUTTON" || c.tagName === "INPUT" || c.tagName === "SELECT");
    }
    return card._collect().filter((c) => c._key === s);
  };
  return { card, viewer, recompute, canvas, blend, nud, tabs, dx };
}

/* ---------------- 加载 rescan.js（注入桩全局） ---------------- */
const srcPath = path.join(__dirname, "..", "static", "rescan.js");
const vm = require("vm");
let code = fs.readFileSync(srcPath, "utf8");
// 截掉文件末尾的 rbBind()（会触碰真实 DOM）
code = code.replace(/\nrbBind\(\);\s*$/, "\n");

const sandbox = {
  console, setTimeout, clearInterval,
  state: { data: { rescan_batches: [] }, reelId: 1 },
  $: (s) => sandbox._els[s] || null,
  api: async () => ({}),
  post: async () => { postCalls++; return sandbox._postResult || {}; },
  toast: () => {},
  loadState: async () => {},
  renderAll: () => {},
  _els: {},
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  document: { querySelector: () => null, querySelectorAll: () => [], createElement: () => new FakeNode("div") },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
// 顶层 const/let 不会挂到上下文对象，用表达式取回需要的符号
const probe = vm.runInContext(
  code + "\n;({rbBindViewer, rbRenderViewer, rbRecompute, rbCard, rbRegUI, rbRegGated, rbRenderCard, rbInternals, rb});",
  sandbox);
const { rbBindViewer, rbRenderViewer, rbRecompute, rbCard, rbRegUI, rbRegGated,
        rbRenderCard } = probe;
const rbModule = probe.rb;
assert(typeof rbBindViewer === "function", "rbBindViewer 已导出（可测试）");
assert(typeof rbRecompute === "function", "rbRecompute 已导出（可测试）");

/* ---------------- 用例 1：换卡后查看器仍可操作 ---------------- */
function makeItem(over) {
  return Object.assign({
    id: 42, status: "pending", frame_no: "33", filename: "f.tif",
    file_id: 1, target_frame_id: 35, block_reason: "", decision_note: "",
    reg: { status: "low", status_label: "相似度偏低", manual: false, force_reason: "",
      rotation: 0, dx: 0, dy: 0, dx_full: 0, dy_full: 0, iou: 0.56, edge_frac: 0,
      lum_mad: 100, auto: true, message: "", scale: 0.12 },
  }, over || {});
}

(async () => {
  // 旧卡片绑定
  let built = buildCard();
  const oldCard = built.card;
  const item = makeItem();
  rbRegUI(item.id);
  rbBindViewer(oldCard, item);
  rbRenderViewer(oldCard, item);
  const oldRecompute = built.recompute;
  assert((oldRecompute._listeners.click || []).length === 1,
    "旧卡片的“应用微调”按钮已绑定点击");
  const oldBlend = built.blend;
  assert((oldBlend._listeners.input || []).length === 1, "旧卡片叠加滑杆已绑定");

  // 模拟 rbRecompute：服务端返回重算后的 detail；rbCard 生成新卡片后必须重绑+重渲染。
  const itemFresh = makeItem({ reg: Object.assign({}, item.reg, { dx_full: 25, dy: 3, iou: 0.7 }) });
  sandbox._postResult = { detail: { items: [itemFresh] } };
  // 预先构造一个带完整查看器控件的“新卡片”，通过测试桩让换卡返回它。
  const newCardBuilt = buildCard();
  const newCard = newCardBuilt.card;
  probe.rbInternals.renderCard = () => newCard;
  // dirty 置真，确保按人工微调路径发 manual 参数
  rbRegUI(item.id).dirty = true;
  rbRegUI(item.id).dx_full = 25;
  try {
    await rbRecompute(oldCard, item, true);
  } catch (e) { console.error("rbRecompute threw:", e); }

  assert((newCardBuilt.recompute._listeners.click || []).length === 1,
    "换卡后新卡片的“应用微调”按钮仍可点击（已重新绑定）");
  assert((newCardBuilt.blend._listeners.input || []).length === 1,
    "换卡后新卡片的叠加滑杆仍可拖动（已重新绑定）");
  const tabBtns = newCard.querySelectorAll(".rb-reg-tabs button");
  const boundTabs = tabBtns.filter((b) => (b._listeners.click || []).length === 1);
  eq(boundTabs.length, 3, "换卡后叠加/闪烁/差异图三个切换标签均重新绑定");
  assert(newCardBuilt.canvas._html.indexOf("registration.") !== -1,
    "换卡后查看器已重新渲染（canvas 写入配准图 URL）");

  /* ---------------- 用例 2：空 reg 状态被门禁判定为需强制 ---------------- */
  const gated = rbRegGated;
  assert(gated(makeItem({ reg: null })) === true, "无配准记录(null) -> 需要强制/不得批量");
  assert(gated(makeItem({ reg: { status: "" } })) === true, "空 reg_status -> 需要强制/不得批量");
  assert(gated(makeItem({ reg: { status: "low" } })) === true, "low -> 需要强制");
  assert(gated(makeItem({ reg: { status: "failed" } })) === true, "failed -> 需要强制");
  assert(gated(makeItem({ reg: { status: "no_original" } })) === true, "no_original -> 需要强制");
  assert(gated(makeItem({ reg: { status: "ok" } })) === false, "ok -> 可直接/批量接受");

  if (failures) { console.error(`\n${failures} 项失败`); process.exit(1); }
  console.log("\n全部通过");
})();
