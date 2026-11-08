/* STOCKMON front-end — vanilla JS, no dependencies.
   Lucide icons vendored in icons.js (window.LUCIDE). No emoji anywhere. */
"use strict";

/* ---------------- icons ---------------- */
const ICON_NS = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
function iconSvg(name) {
  const body = (window.LUCIDE && window.LUCIDE[name]) || window.LUCIDE["circle-dot"] || "";
  return `<svg ${ICON_NS}>${body}</svg>`;
}
function hydrateIcons(root) {
  (root || document).querySelectorAll("[data-icon]").forEach(el => {
    if (!el.firstElementChild) el.innerHTML = iconSvg(el.dataset.icon);
  });
}

/* ---------------- utils ---------------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const FINE = matchMedia("(pointer: fine)").matches;

function fmtPrice(v) {
  if (v == null || isNaN(v)) return "--";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtSigned(v, digits = 2) {
  if (v == null || isNaN(v)) return "--";
  const n = Number(v);
  return (n > 0 ? "+" : "") + n.toFixed(digits) + "%";
}
function chgClass(v) { return v == null || isNaN(v) || Number(v) === 0 ? "flat" : (Number(v) > 0 ? "up" : "down"); }
function fmtTime(ts) {
  if (!ts) return "--";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const err = new Error(data.detail || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------------- canvas backdrop ----------------
   两种模式：数据模式（自选标的真实 90 日走势叠加，警戒时泛红）
   与 ambient 模式（未登录/无数据时的装饰性年线场）。 */
let backdropSeries = null;   // [{code, name, ret, series[0..1]}]
let alertMode = false;
let backdropMode = "ambient";        // ambient | dash | detail
let detailSeries = null, detailAlert = false, detailRet = 0;
let chartState = null, lastDetailPrice = null;

function startBackdrop() {
  const cv = $("#bg");
  const ctx = cv.getContext("2d");
  let W, H, dpr;
  let px = 0, py = 0, tx = 0, ty = 0;

  function resize() {
    dpr = Math.min(devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function grid() {
    ctx.strokeStyle = "rgba(234,231,224,.045)";
    ctx.lineWidth = 1;
    const step = Math.max(90, Math.min(150, W / 12));
    ctx.beginPath();
    for (let x = (px * .4) % step; x < W; x += step) { ctx.moveTo(x, 0); ctx.lineTo(x, H); }
    for (let y = (py * .4) % step; y < H; y += step) { ctx.moveTo(0, y); ctx.lineTo(W, y); }
    ctx.stroke();
  }
  function drawData(t) {
    const src = backdropSeries || [];
    const n = Math.min(src.length, 8);
    const bandH = (H - 140) / Math.max(1, n);
    for (let i = 0; i < n; i++) {
      const s = src[i];
      const arr = s.series;
      const top = 70 + i * bandH;
      const sway = Math.sin(t * .0004 + i * 1.4) * 5;
      let color;
      if (alertMode) color = "rgba(255,64,52,.30)";
      else if (s.ret >= 0) color = "rgba(255,84,73,.17)";
      else color = "rgba(46,194,126,.15)";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      for (let x = -10; x <= W + 10; x += 7) {
        const u = Math.min(.9999, Math.max(0, x / W));
        const v = arr[Math.floor(u * (arr.length - 1))];
        const y = top + bandH * (1 - v) + sway + py * (.4 + i * .08);
        x <= -10 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }
  function drawAmbient(t) {
    const palette = ["rgba(234,231,224,.10)", "rgba(255,84,73,.13)", "rgba(46,194,126,.10)",
                     "rgba(234,231,224,.07)", "rgba(255,178,36,.08)"];
    for (let i = 0; i < 5; i++) {
      const amp = 40 + i * 26, speed = .00006 + i * .00004, phase = i * 1.7;
      const yBase = .22 + i * .16;
      ctx.strokeStyle = alertMode ? "rgba(255,64,52,.22)" : palette[i];
      ctx.lineWidth = i === 0 ? 1.4 : 1;
      ctx.beginPath();
      for (let x = -20; x <= W + 20; x += 14) {
        const u = x / W;
        const y = H * yBase + py * (.5 + yBase)
          + Math.sin(u * 6.3 + phase + t * speed) * amp
          + Math.sin(u * 17.3 - t * speed * 1.7 + phase) * amp * .3;
        x === -20 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }
  function drawDetail(t) {
    const arr = detailSeries || [];
    if (!arr.length) return;
    const sway = Math.sin(t * .0004) * 6;
    let color;
    if (detailAlert || alertMode) color = "rgba(255,64,52,.28)";
    else if (detailRet >= 0) color = "rgba(255,84,73,.16)";
    else color = "rgba(46,194,126,.14)";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    for (let x = -10; x <= W + 10; x += 6) {
      const u = Math.min(.9999, Math.max(0, x / W));
      const v = arr[Math.floor(u * (arr.length - 1))];
      const y = H * .55 - (v - .5) * H * .42 + sway + py * .5;
      x <= -10 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  function draw(t) {
    ctx.clearRect(0, 0, W, H);
    px += (tx - px) * .05; py += (ty - py) * .05;
    grid();
    if (backdropMode === "detail" && detailSeries) drawDetail(t);
    else if (backdropSeries && backdropSeries.length) drawData(t);
    else drawAmbient(t);
  }
  let raf, running = true;
  function loop(t) { draw(t); raf = requestAnimationFrame(loop); }
  resize();
  if (REDUCED) { draw(0); }
  else { raf = requestAnimationFrame(loop); }
  addEventListener("resize", () => { resize(); if (REDUCED) draw(0); });
  if (FINE && !REDUCED) {
    addEventListener("pointermove", e => {
      tx = (e.clientX / innerWidth - .5) * 24;
      ty = (e.clientY / innerHeight - .5) * 24;
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (REDUCED) return;
    if (document.hidden) { cancelAnimationFrame(raf); running = false; }
    else if (!running) { running = true; raf = requestAnimationFrame(loop); }
  });
}

let backdropAt = 0;
async function loadBackdrop() {
  if (!me) return;
  const now = Date.now();
  if (backdropSeries && now - backdropAt < 300000) return;
  try {
    const r = await api("/api/backdrop");
    backdropAt = now;
    backdropSeries = (r.series && r.series.length) ? r.series : null;
  } catch (_) { /* keep ambient mode */ }
}

function applyAlertMode(dash) {
  const eff = Object.values(dash.groups || {}).flat()
    .filter(i => i.enabled && i.status === "effective").length;
  alertMode = eff > 0;
  document.body.classList.toggle("alert-mode", alertMode);
}

/* ---------------- motion helpers ---------------- */
function magnetic(sel) {
  if (!FINE || REDUCED) return;
  $$(sel).forEach(el => {
    el.addEventListener("pointermove", e => {
      const r = el.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      el.style.transform = `translate(${dx * .18}px, ${dy * .28}px)`;
    });
    el.addEventListener("pointerleave", () => { el.style.transform = ""; });
  });
}
function countUp(el, to, digits, suffix) {
  const from = parseFloat(el.dataset.v || "0") || 0;
  const target = parseFloat(to);
  el.dataset.v = isNaN(target) ? "" : target;
  if (REDUCED || isNaN(target) || from === target) {
    el.textContent = isNaN(target) ? "--" : target.toFixed(digits) + (suffix || "");
    return;
  }
  const t0 = performance.now(), dur = 760;
  const c1 = 1.70158, c3 = c1 + 1;   /* easeOutBack：轻微过冲回弹 */
  function frame(t) {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2);
    el.textContent = (from + (target - from) * e).toFixed(digits) + (suffix || "");
    if (p < 1) requestAnimationFrame(frame);
    else el.textContent = target.toFixed(digits) + (suffix || "");
  }
  requestAnimationFrame(frame);
}
function switchView(fromEl, toEl) {
  if (document.startViewTransition && !REDUCED) {
    document.startViewTransition(() => {
      fromEl.classList.remove("is-active"); toEl.classList.add("is-active");
    });
  } else {
    fromEl.classList.remove("is-active"); toEl.classList.add("is-active");
  }
}

/* ---------------- auth ---------------- */
let me = null;
function showAuth() {
  $("#view-auth").hidden = false;
  $("#view-app").hidden = true;
  $("#auth-error").hidden = true;
  positionTabInk();
  detailSeries = null;
  backdropMode = "ambient";
}
function showApp() {
  $("#view-auth").hidden = true;
  $("#view-app").hidden = false;
}
function positionTabInk() {
  const active = $(".auth-tab.is-active");
  const ink = $(".tab-ink");
  if (active && ink) {
    ink.style.left = active.offsetLeft + "px";
    ink.style.width = active.offsetWidth + "px";
  }
}

let authMode = "login";
function initAuth() {
  $$(".auth-tab").forEach(btn => btn.addEventListener("click", () => {
    if (btn.dataset.auth === authMode) return;
    authMode = btn.dataset.auth;
    $$(".auth-tab").forEach(b => b.classList.toggle("is-active", b === btn));
    positionTabInk();
    $("#auth-submit-label").textContent = authMode === "login" ? "进入系统" : "创建账号";
    $("#auth-error").hidden = true;
  }));
  $("#auth-form").addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = { username: (fd.get("username") || "").trim(), password: fd.get("password") || "" };
    const errEl = $("#auth-error");
    errEl.hidden = true;
    const submit = $("#auth-submit");
    submit.disabled = true;
    try {
      const r = await api(authMode === "login" ? "/api/login" : "/api/register",
                          { method: "POST", body });
      if (r.pending) {
        errEl.textContent = r.message || "注册成功，等待管理员审核";
        errEl.classList.add("info");
        errEl.hidden = false;
        toast("注册成功，等待管理员审核");
        // switch back to the login tab for later sign-in
        $$(".auth-tab")[0].click();
        $('input[name="username"]', e.target).value = r.username || body.username;
        return;
      }
      errEl.classList.remove("info");
      await boot();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("info");
      errEl.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  addEventListener("resize", positionTabInk);
}

/* ---------------- labels ---------------- */
const MARKETS = {
  cn_stock:  { title: "A股", num: "01" },
  cn_index:  { title: "A股指数", num: "02" },
  us_stock:  { title: "美股", num: "03" },
  us_index:  { title: "美股指数", num: "04" },
};
const STATUS = {
  above:     { label: "年线上方", cls: "badge-above",
               tip: "收盘价 ≥ 年线 MA250（250 个交易日收盘均价，前复权口径）" },
  pending:   { label: "年线下方 · 待确认", cls: "badge-pending",
               tip: "收盘价跌破年线，但跌破当日未同时满足「量比>1.5 且 周线MACD死叉」，不计入破位天数" },
  day1:      { label: "破位第 1 天", cls: "badge-day" },
  day2:      { label: "破位第 2 天", cls: "badge-day" },
  effective: { label: "有效破位", cls: "badge-effective",
               tip: "收盘破位已持续 ≥3 个交易日（确认起点需当日量比>1.5 且周线死叉）" },
};
/* 状态词单一出口：兼容 day1..dayN 任意计数，后端词表变化不影响显示 */
function statusMeta(status) {
  if (!status) return null;
  const m = /^day(\d+)$/.exec(String(status));
  if (m) return { label: `破位第 ${m[1]} 天`, cls: "badge-day",
    tip: `收盘跌破年线且当日量比>1.5、周线MACD死叉——连续第 ${m[1]} 个收盘日破位；满 3 天判定为有效破位` };
  return STATUS[status] || null;
}
function statusBadge(status, enabled, extraCls) {
  if (!enabled) return `<span class="badge badge-above${extraCls ? " " + extraCls : ""}" data-tip="已停用：不参与监控与收盘确认"><span class="dot"></span>已停用</span>`;
  const s = statusMeta(status) || { label: "等待收盘", cls: "badge-above",
    tip: "尚无收盘轮数据，等待第一次收盘确认" };
  return `<span class="badge ${s.cls}${extraCls ? " " + extraCls : ""}"${s.tip ? ` data-tip="${s.tip}"` : ""}><span class="dot"></span>${s.label}</span>`;
}
/* 收盘轮尚未覆盖时，用现价 vs MA250 给出临时徽章（明确标注待确认）；
   检测器收盘确认后自动切换为正式状态徽章 */
function statusOrProvisional(status, enabled, price, ma250, extraCls) {
  if (!enabled) return statusBadge(status, false, extraCls);
  if (status) return statusBadge(status, true, extraCls);
  if (price != null && ma250) {
    return price >= ma250
      ? `<span class="badge badge-above${extraCls ? " " + extraCls : ""}" data-tip="收盘轮尚未覆盖：按现价与年线的即时对比显示，以每日收盘确认为准"><span class="dot"></span>年线上方 · 待收盘确认</span>`
      : `<span class="badge badge-pending${extraCls ? " " + extraCls : ""}" data-tip="现价在年线下方；是否计入破位天数以收盘确认为准"><span class="dot"></span>年线下方 · 待确认</span>`;
  }
  return statusBadge(null, true, extraCls);
}
function macdBadge(weeklyMacd) {
  const dead = weeklyMacd === "dead_cross";
  const tip = dead
    ? "周线MACD DIF 下穿 DEA——中期动能转弱，是破位确认的必要条件之一"
    : "周线MACD DIF 在 DEA 上方——中期动能未见转弱";
  return `<span class="badge ${dead ? "badge-pending" : "badge-above"}" data-tip="${tip}"><span class="dot"></span>周线${dead ? "死叉" : "未死叉"}</span>`;
}
const SELL = {
  resistance_stall: "放量滞涨",
  top_divergence: "周线顶背离",
  ma10_break: "跌破10日线",
};
const SELL_TIP = {
  resistance_stall: "现价距年线 ±3% 以内或距 60 日高点 3% 以内，且量比>1.5、当日涨幅<+1%——关键位置放量却不涨",
  top_divergence: "收盘创 8 周新高，但周线MACD DIF 低于近 8 周峰值——价格新高而动能未跟",
  ma10_break: "收盘跌破 10 日均线（MA10），且量比>1.5、当日下跌——放量破位下跌",
};
const BUY = {
  recross_confirm: "站回年线3日确认",
  ma250_pullback: "回踩年线缩量企稳",
  breakout_high60: "放量突破60日新高",
};
const BUY_TIP = {
  recross_confirm: "收复年线后连续 3 个收盘日站上年线",
  ma250_pullback: "收盘 ≥ 年线且距其 2% 以内，量比<1.0——缩量回踩不破",
  breakout_high60: "收盘创 60 日新高 + 量比>1.5 + 站上年线——放量突破",
};
function sellBadge(sig) {
  if (!sig || sig === "none") return "";
  return `<span class="badge badge-sell" data-tip="${SELL_TIP[sig] || ""}"><span class="dot"></span>止赢 · ${SELL[sig] || sig}</span>`;
}
function buyBadge(buy) {
  if (!buy || buy === "none") return "";
  return `<span class="badge badge-buy" data-tip="${BUY_TIP[buy] || ""}"><span class="dot"></span>加仓 · ${BUY[buy] || buy}</span>`;
}
/* 短线徽章：日线MACD状态 + RSI 区间组合，青色系 */
const ST_TIP = {
  "金叉": "日线MACD DIF > DEA——短线多头状态",
  "死叉": "日线MACD DIF < DEA——短线空头状态",
  "超卖": "RSI(14) < 30——短线超卖区",
  "超买": "RSI(14) > 70——短线超买区",
};
function stTipOf(parts) {
  return parts.map(p => ST_TIP[p]).filter(Boolean).join("；");
}
function stBadge(i) {
  if (!i.enabled) return "";
  const parts = [];
  if (i.st_macd === "golden") parts.push("金叉");
  if (i.st_macd === "dead") parts.push("死叉");
  if (i.st_rsi === "oversold") parts.push("超卖");
  if (i.st_rsi === "overbought") parts.push("超买");
  if (!parts.length) return "";
  return `<span class="badge badge-st" data-tip="${stTipOf(parts)}"><span class="dot"></span>短线 · ${parts.join(" · ")}</span>`;
}
const EVENT_TYPE = t => {
  const map = {
    intraday_warning: "盘中跌破预警", intraday_recross: "盘中站回（记录）",
    close_break_day1: "破位第 1 天", close_break_pending: "跌破年线 · 待确认",
    close_recross_release: "站回年线", effective_break: "有效破位",
    close_take_profit: "止赢信号", close_add_position: "加仓信号",
    st_macd_golden: "短线MACD金叉 · 买", st_macd_dead: "短线MACD死叉 · 卖",
    st_rsi_oversold_in: "RSI进入超卖区 · 买", st_rsi_oversold_out: "RSI离开超卖区 · 回升",
    st_rsi_overbought_in: "RSI进入超买区 · 卖", st_rsi_overbought_out: "RSI离开超买区 · 回落",
    index_intraday_warning: "指数盘中跌破预警", index_intraday_recross: "指数盘中站回（记录）",
    index_close_break_day1: "指数破位第 1 天", index_close_break_pending: "指数跌破年线 · 待确认",
    index_close_recross_release: "指数站回年线", index_effective_break: "指数有效破位",
    us_intraday_warning: "美股盘中跌破预警", us_intraday_recross: "美股盘中站回（记录）",
    us_close_break_day1: "美股破位第 1 天", us_close_break_pending: "美股跌破年线 · 待确认",
    us_close_recross_release: "美股站回年线", us_effective_break: "美股有效破位",
  };
  return map[t] || t;
};
const EVENT_SEV = t => {
  if (/effective|macd_dead|overbought_in/.test(t)) return "sev-danger";
  if (/oversold|golden/.test(t)) return "sev-ok";
  if (/overbought_out/.test(t)) return "sev-warn";
  if (/add_position/.test(t)) return "sev-ok";
  if (/take_profit/.test(t)) return "sev-sell";
  if (/recross|release/.test(t)) return "sev-ok";
  if (/warning|break/.test(t)) return "sev-warn";
  return "";
};

/* ---------------- dashboard ---------------- */
let dashCache = null;
let dashLoadSeq = 0;
async function loadDashboard() {
  /* 三击式首屏：快照先出、完整数据后台替换、迷你分时最后补画 */
  const seq = ++dashLoadSeq;
  /* 第一击：纯库快照（fast=1，亚百毫秒）——名字/价格/状态徽章立即渲染 */
  try {
    const fast = await api("/api/dashboard?fast=1");
    if (seq === dashLoadSeq) {
      dashCache = fast;
      backdropMode = (backdropSeries && backdropSeries.length) ? "dash" : "ambient";
      renderDashboard(fast);
    }
  } catch (_) { /* 快照失败则直接等完整版 */ }
  /* 第二击：完整数据（实时短线状态 + 开市行情合并）后台到达后替换 */
  try {
    const data = await api("/api/dashboard");
    if (seq !== dashLoadSeq) return;
    dashCache = data;
    backdropMode = (backdropSeries && backdropSeries.length) ? "dash" : "ambient";
    renderDashboard(data);
    applyQuotes();
  } catch (_) { /* 保留首屏快照 */ }
  /* 第三击：backdrop/迷你分时到达后只重画小图，不重建 DOM */
  try {
    await loadBackdrop();
    if (seq !== dashLoadSeq) return;
    backdropMode = (backdropSeries && backdropSeries.length) ? "dash" : "ambient";
    renderSparks();
  } catch (_) { /* 保持 ambient 背景 */ }
}
function renderDashboard(data) {
  const groups = data.groups || {};
  const all = Object.values(groups).flat();
  const enabled = all.filter(i => i.enabled);

  /* hero */
  const eff = enabled.filter(i => i.status === "effective").length;
  const day = enabled.filter(i => /^day\d$/.test(i.status || "")).length;
  const pend = enabled.filter(i => i.status === "pending").length;
  const hero = $("#hero-title");
  const heroHTML = !all.length
    ? `添加自选<br><span class="t-outline">开始监控</span>`
    : eff ? `<span class="hl-up">${eff}</span> 项<span class="t-outline">有效破位</span>`
    : day ? `<span class="hl-warn">${day}</span> 项<span class="t-outline">破位确认中</span>`
    : pend ? `<span class="hl-warn">${pend}</span> 项<span class="t-outline">待确认</span>`
    : `${enabled.length} 项<span class="t-outline">监控中</span>`;
  if (hero._html !== heroHTML) { hero.innerHTML = heroHTML; hero._html = heroHTML; }

  const lastSnap = all.map(i => i.updated_at).filter(Boolean).sort().pop();
  $("#hero-meta-text").textContent =
    `系统监测中 · 上次收盘快照 ${lastSnap ? fmtTime(lastSnap) : "待首次收盘"}`;
  $("#hero-sub").textContent = !all.length
    ? "前往自选页添加 A 股或美股标的，系统将自动计算年线与周线 MACD"
    : `年线 MA250 + 量比 1.5 + 周线死叉 + 3 日确认 · 共 ${enabled.length} 项启用 / ${all.length} 项`;
  $(".live-dot").classList.toggle("alert", eff > 0);

  /* groups */
  const wrap = $("#dash-groups");
  const sections = [
    { key: "cn_stock", items: groups.cn_stock || [] },
    { key: "cn_index", items: groups.cn_index || [] },
    { key: "us_stock", items: groups.us_stock || [] },
    { key: "us_index", items: groups.us_index || [] },
  ];
  wrap.innerHTML = sections.map(sec => {
    if (!sec.items.length) return "";
    const m = MARKETS[sec.key] || { title: "美股", num: "03" };
    return `
    <div class="group" data-filter="${sec.key}">
      <div class="sec-head">
        <span class="sec-num">${m.num}</span>
        <h2 class="sec-title">${m.title}</h2>
        <span class="sec-desc">${sec.items.filter(i => i.enabled).length}/${sec.items.length} 启用</span>
      </div>
      ${sec.items.map(itemHTML).join("")}
    </div>`;
  }).join("") || `
    <div class="empty-state">
      <span class="ic">${iconSvg("radar")}</span><br>
      暂无自选标的<button data-goto="watchlist">前往添加</button>
    </div>`;
  hydrateIcons(wrap);
  $$("[data-goto]", wrap).forEach(b => b.addEventListener("click",
    () => nav("watchlist")));
  applyDashFilter();
  applyAlertMode(data);
  renderSparks();

  /* ticker */
  const tk = $("#ticker");
  if (all.length) {
    tk.hidden = false;
    const seg = all.map(i =>
      `<span class="tk"><span class="tk-name">${esc(i.name)}</span>` +
      `<span class="tk-price num">${fmtPrice(i.price)}</span>` +
      `<span class="tk-chg num ${chgClass(i.day_change_pct)}">${fmtSigned(i.day_change_pct)}</span></span>` +
      `<span class="tk-sep"></span>`).join("");
    $("#ticker-track").innerHTML = seg + seg;
  } else { tk.hidden = true; }

  animatePrices();
  magnetic(".btn-magnetic");
}
function itemHTML(i) {
  /* 临时判定与正式 side 字段同口径：收盘确认前按现价 vs MA250 推断 */
  const below = i.side === "below" ||
    (i.status == null && i.price != null && i.ma250 && i.price < i.ma250);
  /* dist 语义：跌破年线（负值）用警示色调 */
  const distColor = i.status === "effective" || /^day/.test(i.status || "")
    ? "up" : (below ? "up" : "");
  const sell = i.enabled && ((i.sell_signal && i.sell_signal !== "none")
    || (i.buy_signal && i.buy_signal !== "none")
    || i.st_note) ? `
    <div class="item-sell">
      ${sellBadge(i.sell_signal)}
      ${buyBadge(i.buy_signal)}
      ${i.sell_note ? `<span class="sell-note">${esc(i.sell_note)}</span>`
        : i.buy_note ? `<span class="sell-note">${esc(i.buy_note)}</span>`
        : i.st_note ? `<span class="sell-note">${esc(i.st_note)}</span>` : ""}
    </div>` : "";
  return `
  <article class="item enter" data-iid="${i.id}" data-code="${esc(i.code)}">
    <div class="item-id">
      <span class="item-name">${esc(i.name)}</span>
      <span class="item-code">${esc(i.code)}</span>
      <canvas class="spark" data-code="${esc(i.code)}" width="240" height="52"></canvas>
    </div>
    <div class="item-st">${stBadge(i)}</div>
    <div class="item-price">
      <b class="num" data-price="${i.price ?? ""}"><span class="item-live" hidden></span>${fmtPrice(i.price)}</b>
      <span class="item-chg ${chgClass(i.day_change_pct)}">${fmtSigned(i.day_change_pct)}</span>
    </div>
    <div class="item-ma">
      <span class="ma-dist num ${distColor}">${fmtSigned(distOf(i))}</span>
      <span class="ma-cap">距年线 · 年线 <span class="num">${fmtPrice(i.ma250)}</span></span>
      <span class="gauge"><i style="left:${Math.max(3, Math.min(97, 50 + (distOf(i) || 0) / 30 * 50)).toFixed(1)}%"></i></span>
    </div>
    <div class="item-status">
      ${statusOrProvisional(i.status, i.enabled, i.price, i.ma250)}
    </div>
    ${sell}
  </article>`;
}
/* dashboard rows carry no dist_pct; derive from price vs ma250 when both exist */
function distOf(i) {
  if (i.dist_pct != null) return i.dist_pct;
  if (i.price != null && i.ma250) return (i.price / i.ma250 - 1) * 100;
  return null;
}

function animatePrices() {
  $$("#dash-groups [data-price]").forEach(el => {
    const v = parseFloat(el.dataset.price);
    if (!isNaN(v)) countUp(el, v, 2);
  });
}

/* ---------------- dashboard market filter (multi-select toggles) ----------------
   dashSel === null -> 全部；Set -> 只显示选中的市场；点亮的再点一次取消，
   全部取消后自动回到「全部」。 */
let dashSel = null;
function applyDashFilter() {
  $$("#dash-groups .group").forEach(g => {
    g.hidden = !(dashSel === null || dashSel.has(g.dataset.filter));
  });
  $$("#dash-filter .seg-btn").forEach(b => {
    const f = b.dataset.filter;
    b.classList.toggle("is-active",
      f === "all" ? dashSel === null : !!dashSel && dashSel.has(f));
  });
  const anyVisible = $$("#dash-groups .group:not([hidden])").length > 0;
  const anyAtAll = $$("#dash-groups .group").length > 0;
  $("#dash-empty-note").hidden = anyVisible || !anyAtAll;
}

/* 行内迷你走势图：优先当日分时（红涨绿跌相对昨收），无分时回退 90 日日线 */
function renderSparks() {
  if (!backdropSeries) return;
  const byCode = {};
  backdropSeries.forEach(s => { byCode[s.code] = s; });
  $$("#dash-groups .spark").forEach(cv => {
    const s = byCode[cv.dataset.code];
    if (!s) { cv.style.display = "none"; return; }
    cv.style.display = "block";
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = 120, H = 26;
    cv.width = W * dpr; cv.height = H * dpr;
    const c = cv.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, W, H);
    let arr, base = null;
    if (s.intraday && s.intraday.pts && s.intraday.pts.length >= 2) {
      arr = s.intraday.pts.map(pt => pt[1]);
      base = s.intraday.preClose;
    } else {
      arr = s.series.slice(-90);
      base = arr[0];
    }
    /* 昨收基准虚线（分时模式） */
    if (base != null && s.intraday) {
      const lo = Math.min(...arr, base), hi = Math.max(...arr, base);
      const span = (hi - lo) || 1;
      const by = 3 + (H - 6) * (1 - (base - lo) / span);
      c.setLineDash([2, 3]);
      c.strokeStyle = "rgba(234,231,224,.3)";
      c.beginPath(); c.moveTo(2, by); c.lineTo(W - 2, by); c.stroke();
      c.setLineDash([]);
    }
    const up = base != null ? arr[arr.length - 1] >= base : s.ret >= 0;
    const lo = Math.min(...arr, base != null ? base : arr[0]);
    const hi = Math.max(...arr, base != null ? base : arr[0]);
    const span = (hi - lo) || 1;
    c.strokeStyle = up ? "rgba(255,84,73,.85)" : "rgba(46,194,126,.85)";
    c.lineWidth = 1.2;
    c.beginPath();
    arr.forEach((v, i) => {
      const x = 2 + (W - 4) * i / Math.max(1, arr.length - 1);
      const y = 3 + (H - 6) * (1 - (v - lo) / span);
      i ? c.lineTo(x, y) : c.moveTo(x, y);
    });
    c.stroke();
  });
}

/* 骨架屏：数据到达前占位 */
function renderSkeleton() {
  const item = `<div class="item">
    <div><div class="sk sk-id"></div></div>
    <div class="sk sk-badge"></div>
    <div class="sk sk-num"></div>
    <div class="sk sk-num"></div>
    <div class="sk sk-badge"></div>
  </div>`;
  $("#dash-groups").innerHTML =
    `<div class="group"><div class="sec-head"><span class="sec-num">01</span>` +
    `<h2 class="sec-title">A股</h2></div>${item}${item}${item}</div>`;
}

/* ---------------- timeline ---------------- */
function renderTimeline(el, evs, markFirst) {
  if (!evs || !evs.length) {
    el.innerHTML = `<li class="tl-empty">暂无事件 —— 检测器会在跌破年线、止赢信号触发等时刻在这里记录</li>`;
    return;
  }
  const prevFirst = markFirst ? el.dataset.first : null;
  el.innerHTML = evs.map(e => `
    <li class="tl-item ${EVENT_SEV(e.event_type)}${e.id + "" === prevFirst ? "" : (markFirst ? " enter" : "")}">
      <span class="tl-dot"></span>
      <div class="tl-top">
        <span class="tl-time">${fmtTime(e.ts)}</span>
        <span class="tl-type">${EVENT_TYPE(e.event_type)}</span>
        <span class="tl-name">${esc(e.name)} <span class="num">${esc(e.code)}</span></span>
      </div>
      <div class="tl-body">现价 ${fmtPrice(e.price)} · 距年线 ${fmtSigned(distOf(e))} · 当日 ${fmtSigned(e.day_change_pct)}${e.volume_ratio ? ` · 量比 ${Number(e.volume_ratio).toFixed(2)}` : ""}</div>
      ${e.note ? `<p class="tl-note">${esc(e.note)}</p>` : ""}
    </li>`).join("");
  if (markFirst && evs.length) el.dataset.first = evs[0].id + "";
}
async function loadEvents() {
  const data = await api("/api/events?limit=60");
  renderTimeline($("#timeline"), data.events || [], true);
}

/* ---------------- watchlist ---------------- */
let addMarket = "cn_stock";
async function loadWatchlist() {
  const data = await api("/api/instruments");
  const wrap = $("#wl-list");
  const items = data.items || [];
  if (!items.length) {
    wrap.innerHTML = `<div class="empty-state"><span class="ic">${iconSvg("list")}</span><br>还没有自选 —— 用上方面板添加第一项</div>`;
    return;
  }
  wrap.innerHTML = items.map(i => {
    const m = MARKETS[i.market] || { title: i.market };
    const below = i.side === "below";
    const dist = i.last_price != null && i.ma250 ? ((i.last_price / i.ma250 - 1) * 100) : null;
    return `
    <div class="wl-row${i.enabled ? "" : " disabled"}" data-wid="${i.id}">
      <div class="wl-id">
        <span class="item-name">${esc(i.name)}</span>
        <span class="item-code">${esc(i.code)} · ${m.title}${i.weekly_macd === "dead_cross" ? " · 周线死叉" : ""}</span>
      </div>
      <div class="wl-ma"><small>MA250</small>${fmtPrice(i.ma250)} <span class="${dist != null && dist < 0 ? "up" : ""}">${fmtSigned(dist)}</span></div>
      ${statusOrProvisional(i.status, i.enabled, i.last_price, i.ma250, "wl-status")}
      <button class="switch${i.enabled ? " on" : ""}" title="${i.enabled ? "停用" : "启用"}" aria-label="启停"><i></i></button>
      <button class="del-btn" title="删除"><span class="ic">${iconSvg("trash-2")}</span></button>
    </div>`;
  }).join("");

  $$(".wl-row", wrap).forEach(row => {
    const id = row.dataset.wid;
    row.addEventListener("click", e => {
      if (e.target.closest(".switch") || e.target.closest(".del-btn")) return;
      openDetail(id);
    });
    $(".switch", row).addEventListener("click", async () => {
      const on = $(".switch", row).classList.contains("on");
      try {
        await api(`/api/instruments/${id}`, { method: "PATCH", body: { enabled: on ? 0 : 1 } });
        loadWatchlist();
      } catch (e) { toast(e.message, true); }
    });
    const del = $(".del-btn", row);
    del.addEventListener("click", async () => {
      if (!del.classList.contains("arm")) {
        del.classList.add("arm");
        del.innerHTML = `<span class="ic">${iconSvg("check")}</span>`;
        setTimeout(() => {
          del.classList.remove("arm");
          del.innerHTML = `<span class="ic">${iconSvg("trash-2")}</span>`;
        }, 2600);
        return;
      }
      row.classList.add("removing");
      try {
        await api(`/api/instruments/${id}`, { method: "DELETE" });
        setTimeout(() => { loadWatchlist(); toast("已删除"); }, 320);
      } catch (e) { toast(e.message, true); row.classList.remove("removing"); }
    });
  });
}

function initAddForm() {
  $$(".seg-btn").forEach(b => b.addEventListener("click", () => {
    addMarket = b.dataset.market;
    $$(".seg-btn").forEach(x => x.classList.toggle("is-active", x === b));
    renderChips();
  }));
  $("#add-form").addEventListener("submit", async e => {
    e.preventDefault();
    const code = $("#add-code").value.trim();
    const out = $("#add-result");
    if (!code) return;
    const btn = $("#add-submit");
    btn.disabled = true;
    out.hidden = false;
    out.className = "add-result";
    out.textContent = "解析代码并计算基线……";
    try {
      const r = await api("/api/instruments", { method: "POST", body: { market: addMarket, code } });
      const b = r.computed;
      out.innerHTML = `
        已添加 <b>${esc(r.item.name)}</b> <span class="num">${esc(r.item.code)}</span><br>
        现价 <span class="num">${fmtPrice(b.price)}</span> ·
        MA250 <span class="num">${fmtPrice(b.ma250)}</span> ·
        周线MACD <span class="num">${b.weekly_macd === "dead_cross" ? "死叉" : "未死叉"}</span> ·
        样本 ${b.bars} 根日K`;
      $("#add-code").value = "";
      loadWatchlist();
      toast("已添加 " + r.item.name);
    } catch (err) {
      out.className = "add-result err";
      out.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

/* ---------------- index catalog chips ---------------- */
let catalogCache = null;
async function loadCatalog() {
  if (!catalogCache) catalogCache = await api("/api/index-catalog");
  return catalogCache;
}
async function renderChips() {
  const box = $("#index-chips");
  if (addMarket !== "cn_index" && addMarket !== "us_index") {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  try {
    const cat = await loadCatalog();
    const list = addMarket === "cn_index" ? cat.cn : cat.us;
    box.innerHTML = list.map(x =>
      `<button type="button" class="chip" data-code="${x.code}">` +
      `<span>${esc(x.name)}</span><span class="num">${x.code}</span></button>`).join("");
    $$(".chip", box).forEach(ch => ch.addEventListener("click", () => {
      $("#add-code").value = ch.dataset.code;
      $("#add-form").requestSubmit();
    }));
  } catch (_) { /* catalog unavailable: manual input still works */ }
}

/* ---------------- live quotes (Sina batch, 15s in-session) ---------------- */
let liveQuotes = {};
function marketPulse() {
  const d = new Date();
  const bj = new Date(d.getTime() + (480 + d.getTimezoneOffset()) * 60000);
  const day = bj.getDay(), mins = bj.getHours() * 60 + bj.getMinutes();
  if (day === 0 || day === 6) return false;
  const cn = (mins >= 570 && mins < 690) || (mins >= 780 && mins < 900);
  const us = mins >= 1290 || mins < 240; /* 粗判，容忍冬夏令时 */
  return cn || us;
}
async function pollQuotes() {
  try {
    const r = await api("/api/quotes");
    liveQuotes = r.quotes || {};
    applyQuotes();
  } catch (_) { /* keep last values */ }
}
function applyQuotes() {
  $$("#dash-groups .item[data-code]").forEach(el => {
    const q = liveQuotes[el.dataset.code];
    if (!q || q.price == null) return;
    const dot = el.querySelector(".item-live");
    if (dot) dot.hidden = false;
    const pe = el.querySelector("[data-price]");
    if (pe) countUp(pe, q.price, 2);
    const chg = el.querySelector(".item-chg");
    if (chg) {
      chg.textContent = fmtSigned(q.day_change_pct);
      chg.className = "item-chg num " + chgClass(q.day_change_pct);
    }
  });
  if (dashCache) {
    const all = Object.values(dashCache.groups || {}).flat();
    if (all.length) {
      const seg = all.map(i => {
        const q = liveQuotes[i.code];
        const price = q && q.price != null ? q.price : i.price;
        const chg = q && q.day_change_pct != null ? q.day_change_pct : i.day_change_pct;
        return `<span class="tk"><span class="tk-name">${esc(i.name)}</span>` +
          `<span class="tk-price num">${fmtPrice(price)}</span>` +
          `<span class="tk-chg num ${chgClass(chg)}">${fmtSigned(chg)}</span></span>` +
          `<span class="tk-sep"></span>`;
      }).join("");
      $("#ticker-track").innerHTML = seg + seg;
    }
  }
  if (currentNav === "detail" && detailItem) {
    const q = liveQuotes[detailItem.code];
    if (q && q.price != null) {
      countUp($("#d-price"), q.price, 2);
      const c = $("#d-chg");
      c.textContent = fmtSigned(q.day_change_pct);
      c.className = "d-chg num " + chgClass(q.day_change_pct);
    }
    const pel = $("#d-price");
    if (q && q.price != null) {
      if (lastDetailPrice != null && q.price !== lastDetailPrice && !REDUCED) {
        pel.classList.remove("flash-up", "flash-down");
        void pel.offsetWidth;
        pel.classList.add(q.price > lastDetailPrice ? "flash-up" : "flash-down");
      }
      countUp(pel, q.price, 2);
      const c = $("#d-chg");
      c.textContent = fmtSigned(q.day_change_pct);
      c.className = "d-chg num " + chgClass(q.day_change_pct);
    }
    lastDetailPrice = q && q.price != null ? q.price : lastDetailPrice;
    renderDetailStats(q, detailItem, detailStatus, false);
  }
}
function startQuotes() {
  let lastFetch = 0;
  setInterval(() => {
    if (document.hidden || !me) return;
    const now = Date.now();
    if (now - lastFetch < (marketPulse() ? 15000 : 60000)) return;
    lastFetch = now;
    pollQuotes();
  }, 3000);
}

/* 详情页三组数据卡：股票信息 / 长线数据 / 短线数据 */
function fmtCap(v) {
  if (v == null || isNaN(v)) return "--";
  const n = Number(v);
  if (n >= 1e12) return (n / 1e12).toFixed(2) + "万亿";
  if (n >= 1e8) return (n / 1e8).toFixed(1) + "亿";
  return n.toLocaleString("en-US");
}
function fmtNum2(v) {
  return v == null || isNaN(v) ? "--" : Number(v).toFixed(2);
}
function fmtPE(v) {
  if (v == null || isNaN(v)) return "--";
  return Number(v) <= 0 ? "亏损" : Number(v).toFixed(1);
}
function renderCells(el, cells, animate) {
  el.innerHTML = cells.map((c, i) =>
    `<div class="d-stat ${c.cls || ""}" style="animation-delay:${i * 45}ms` +
    `${animate ? "" : ";animation:none"}">` +
    `<span class="d-stat-k">${c.k}</span>` +
    `<span class="d-stat-v num">${c.v}</span>` +
    (c.sub ? `<span class="d-stat-sub">${c.sub}</span>` : "") +
    `</div>`).join("");
}
function renderDetailStats(q, item, status, animate) {
  const fund = q || {};
  const infoCells = [
    { k: "今开", v: fmtPrice(fund.open) },
    { k: "昨收", v: fmtPrice(fund.prev_close) },
    { k: "今高", v: fmtPrice(fund.high) },
    { k: "今低", v: fmtPrice(fund.low) },
    { k: "换手率", v: fund.turnover_rate != null
        ? fmtNum2(fund.turnover_rate) + "%" : "--" },
    { k: "收录时间", v: fmtTime(item.created_at) },
  ];
  const isIndex = String(item.market).endsWith("index");
  const vr = fund.volume_ratio != null ? fund.volume_ratio
    : (isIndex && status && status.volume_ratio ? status.volume_ratio : null);
  const ltCells = [
    { k: "年线 MA250", v: fmtPrice(item.ma250),
      sub: "截至 " + (item.ma250_asof || "--"), cls: "ma" },
    { k: isIndex && fund.volume_ratio == null && status && status.volume_ratio
        ? "量比 · 收盘穿越日" : "量比 · 实时",
      v: fmtNum2(vr),
      cls: vr && vr > 1.5 ? "hot" : "" },
    { k: "市盈率TTM", v: fmtPE(fund.pe_ttm) },
    { k: "市净率", v: fmtNum2(fund.pb) },
    { k: "总市值", v: fmtCap(fund.total_cap),
      sub: String(item.market).startsWith("us_") ? "美元" : "人民币" },
    { k: "流通市值", v: fmtCap(fund.float_cap) },
  ];
  renderCells($("#d-stats-info"), infoCells, animate);
  renderCells($("#d-stats-lt"), ltCells, animate);
}

/* ---------------- instrument detail ---------------- */
let detailItem = null;
let detailStatus = null;
let prevNav = "dashboard";
async function loadDetail(id) {
  try {
    const data = await api(`/api/instruments/${id}`);
    detailItem = data.item;
    detailStatus = data.status || {};
    const st = detailStatus;
    $("#d-name").textContent = data.item.name;
    $("#d-code").textContent =
      `${data.item.code} · ${(MARKETS[data.item.market] || {}).title || ""}` +
      (data.item.weekly_macd === "dead_cross" ? " · 周线死叉" : "");
    $("#d-meta").innerHTML =
      `<span class="live-dot ${st.status === "effective" ? "alert" : ""}"></span>` +
      `<span>实时行情 · 破位状态以收盘确认为准</span>`;
    const price = st.price != null ? st.price : data.item.last_price;
    const dist = price != null && data.item.ma250
      ? (price / data.item.ma250 - 1) * 100 : null;
    const pe = $("#d-price");
    pe.dataset.v = "0";
    countUp(pe, price != null ? price : NaN, 2);
    const chg = $("#d-chg");
    chg.textContent = fmtSigned(st.day_change_pct);
    chg.className = "d-chg num " + chgClass(st.day_change_pct);
    const dd = $("#d-dist");
    dd.textContent = fmtSigned(dist);
    dd.className = "hero-title d-dist num" + (dist != null && dist < 0 ? " up" : "");
    /* 长线合并徽章：与短线风格一致（单徽章 + 状态色点），
       止赢/加仓/量比作为信号类徽章仍独立展示 */
    const sMeta = !data.item.enabled
      ? { label: "已停用", cls: "badge-above" }
      : (statusMeta(st.status) ||
         (price != null && data.item.ma250
           ? (price >= data.item.ma250
               ? { label: "年线上方 · 待收盘确认", cls: "badge-above" }
               : { label: "年线下方 · 待确认", cls: "badge-pending" })
           : { label: "待收盘", cls: "badge-above" }));
    const longParts = [sMeta.label,
      data.item.weekly_macd === "dead_cross" ? "周线死叉" : "周线未死叉"];
    const longTip = [sMeta.tip,
      data.item.weekly_macd === "dead_cross"
        ? "周线MACD DIF 下穿 DEA——中期动能转弱，是破位确认的必要条件之一"
        : "周线MACD DIF 在 DEA 上方——中期动能未见转弱"].filter(Boolean).join("；");
    const longBadges =
      `<span class="badge ${sMeta.cls}"${longTip ? ` data-tip="${longTip}"` : ""}><span class="dot"></span>${longParts.join(" · ")}</span>` +
      (data.item.enabled && st.sell_signal && st.sell_signal !== "none"
        ? sellBadge(st.sell_signal)
        : "") +
      (data.item.enabled ? buyBadge(st.buy_signal) : "") +
      (st.volume_ratio
        ? `<span class="badge badge-above"><span class="dot"></span>量比·收盘 ${Number(st.volume_ratio).toFixed(2)}</span>`
        : "");
    const stMacd = st.st_macd || (data.live_st && data.live_st.st_macd) || "";
    const stRsi = st.st_rsi || (data.live_st && data.live_st.st_rsi) || "";
    const stParts = [];
    if (stMacd === "golden") stParts.push("金叉");
    if (stMacd === "dead") stParts.push("死叉");
    if (stRsi === "oversold") stParts.push("超卖");
    if (stRsi === "overbought") stParts.push("超买");
    $("#d-badges").innerHTML =
      `<div class="badge-line"><span class="badge-group-label">长线</span>` + longBadges + `</div>` +
      `<div class="badge-line"><span class="badge-group-label st">短线</span>` +
      (stParts.length
        ? `<span class="badge badge-st" data-tip="${stTipOf(stParts)}"><span class="dot"></span>${stParts.join(" · ")}</span>`
        : `<span class="badge badge-st"><span class="dot"></span>待收盘评估</span>`) +
      `</div>`;
    hydrateIcons($("#panel-detail"));
    renderDetailStats(liveQuotes[data.item.code], data.item, st, true);
    renderTimeline($("#d-timeline"), data.events, false);
    loadChart(id, data.events, st.status === "effective");
    loadIntraday(id);
    pollQuotes();  // fill live price/chg immediately instead of next cycle
  } catch (e) { toast(e.message, true); }
}
async function loadChart(id, events, isEffective) {
  try {
    const k = await api(`/api/instruments/${id}/kline`);
    const closes = k.bars.map(b => b.close);
    const ma5s = rollingMa(closes, 5);
    const ma10s = rollingMa(closes, 10);
    const evDates = (events || [])
      .filter(e => /break|release/.test(e.event_type))
      .map(e => ({ date: String(e.ts).slice(0, 10), type: e.event_type }));
    const cv = $("#d-chart");
    chartState = {
      cv, bars: k.bars, ma250: k.ma250, ma5s, ma10s, evDates,
      dpr: Math.min(devicePixelRatio || 1, 2),
      W: cv.clientWidth || 600, H: cv.clientHeight || 240,
      cross: null,
    };
    /* draw-in 生长动画：仅前台播放；后台打开直接完整呈现（兜底见 finishPendingChart） */
    if (chartState._raf) cancelAnimationFrame(chartState._raf);
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      renderChart(1);
      if (chartState._raf) cancelAnimationFrame(chartState._raf);
      chartState._raf = null;
    };
    chartState._finish = finish;
    if (REDUCED || document.hidden) { finish(); }
    else {
      const t0 = performance.now();
      (function anim(t) {
        if (done) return;
        renderChart(Math.min(1, (t - t0) / 950));
        chartState._raf = requestAnimationFrame(anim);
      })(t0);
      setTimeout(finish, 1200);  /* 后台面板 rAF 停摆的兜底：最迟 1.2s 完整呈现 */
    }
    attachChartInteractions(cv);
    /* 详情页背景联动：该标的自己的走势大线 */
    const lo = Math.min(...closes), hi = Math.max(...closes);
    detailSeries = closes.map(c => (c - lo) / ((hi - lo) || 1));
    detailRet = closes[closes.length - 1] / closes[0] - 1;
    detailAlert = !!isEffective;
    backdropMode = "detail";
    const src = k.source.includes("proxy") ? " · SOX经SMH代理重标定"
      : k.source === "yahoo" ? " · 雅虎" : "";
    $("#d-chart-cap").textContent =
      `近 ${k.bars.length} 个交易日收盘 · 虚线 MA250 · 青线 MA5 · 紫线 MA10${src}`;
    renderStStats(k.st);
  } catch (e) {
    $("#d-chart-cap").textContent = "走势图数据不可用：" + e.message;
  }
}
function rollingMa(closes, n) {
  return closes.map((_, i) => i < n - 1 ? null
    : closes.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n);
}
/* 短线数据网格：与检测器同源指标（MACD状态/DIF/DEA/RSI/MA5/MA10） */
function renderStStats(st) {
  st = st || {};
  const macdTxt = st.macd_state === "golden" ? "金叉"
    : st.macd_state === "dead" ? "死叉" : "--";
  const rsiCls = st.rsi == null ? ""
    : st.rsi > 70 ? "neg" : st.rsi < 30 ? "pos" : "";
  const cells = [
    { k: "日线MACD", v: macdTxt,
      cls: st.macd_state === "golden" ? "pos hl"
        : st.macd_state === "dead" ? "neg hl" : "hl" },
    { k: "DIF", v: fmtNum2(st.dif) },
    { k: "DEA", v: fmtNum2(st.dea) },
    { k: "RSI(14)", v: st.rsi == null ? "--" : Number(st.rsi).toFixed(1),
      cls: rsiCls },
    { k: "MA5", v: fmtPrice(st.ma5) },
    { k: "MA10", v: fmtPrice(st.ma10) },
  ];
  $("#d-st-stats").innerHTML = cells.map(c =>
    `<div class="d-stat ${c.cls || ""}">` +
    `<span class="d-stat-k">${c.k}</span>` +
    `<span class="d-stat-v num">${c.v}</span>` +
    `</div>`).join("");
}
/* 详情页图表渲染器：量柱 + 年线 + MA5/10 + 事件标记 + 十字线 + draw-in */
let chartTipLock = 0;
/* Fritsch-Carlson 单调三次插值：平滑且不过冲（保住高低点真值） */
function smoothPath(ctx, pts) {
  const n = pts.length;
  if (n < 3) {
    if (n) ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < n; i++) ctx.lineTo(pts[i].x, pts[i].y);
    return;
  }
  const dx = [], m = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1].x - pts[i].x);
    m.push((pts[i + 1].y - pts[i].y) / (pts[i + 1].x - pts[i].x));
  }
  const t = [m[0]];
  for (let i = 1; i < n - 1; i++) {
    if (m[i - 1] * m[i] <= 0) t.push(0);
    else {
      const w1 = 2 * dx[i] + dx[i - 1], w2 = dx[i] + 2 * dx[i - 1];
      t.push((w1 + w2) / (w1 / m[i - 1] + w2 / m[i]));
    }
  }
  t.push(m[n - 2]);
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 0; i < n - 1; i++) {
    ctx.bezierCurveTo(
      pts[i].x + dx[i] / 3, pts[i].y + t[i] * dx[i] / 3,
      pts[i + 1].x - dx[i] / 3, pts[i + 1].y - t[i + 1] * dx[i] / 3,
      pts[i + 1].x, pts[i + 1].y);
  }
}

function finishPendingChart() {
  if (chartState && chartState._finish && chartState._p != null && chartState._p < 1) {
    chartState._finish();
  }
}
document.addEventListener("visibilitychange",
  () => { if (!document.hidden) finishPendingChart(); });
addEventListener("scroll", finishPendingChart, { passive: true });
addEventListener("pointerdown", finishPendingChart, { passive: true });

function renderChart(p = 1) {
  const S = chartState;
  if (!S || !S.bars.length) return;
  S._p = Math.min(1, p);
  const { cv, bars, ma250, ma5s, ma10s, cross } = S;
  const ctx = cv.getContext("2d");
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  const W = S.W, H = S.H;
  const closes = bars.map(b => b.close);
  const extras = [ma250, ...(ma5s || []), ...(ma10s || [])]
    .filter(v => v != null && !isNaN(v));
  let lo = Math.min(...closes, ...extras), hi = Math.max(...closes, ...extras);
  const pad = (hi - lo) * .09 || 1; lo -= pad; hi += pad;
  const n = closes.length;
  const xAt = i => 8 + (W - 16) * (i / (n - 1));
  const yAt = v => 12 + (H - 62) * (1 - (v - lo) / (hi - lo));
  const lim = Math.floor((n - 1) * Math.min(1, p));
  /* 画布位图必须与 CSS 尺寸×dpr 对齐，否则绘制会被裁剪在默认 300×150 里 */
  const needW = Math.round(W * S.dpr), needH = Math.round(H * S.dpr);
  if (cv.width !== needW || cv.height !== needH) {
    cv.width = needW; cv.height = needH;
  }
  ctx.clearRect(0, 0, W, H);

  /* 横向网格（点状虚线，更轻） */
  ctx.strokeStyle = "rgba(234,231,224,.04)";
  ctx.setLineDash([2, 7]);
  ctx.beginPath();
  for (let g = 0; g <= 3; g++) {
    const y = 12 + (H - 62) * g / 3;
    ctx.moveTo(0, y); ctx.lineTo(W, y);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  /* 成交量柱：圆角 + sqrt 缩放（尖峰不压扁常规柱） */
  const vols = bars.map(b => b.volume || 0);
  const vmax = Math.max(...vols, 1);
  const vBase = H - 14, vMaxH = H * .14;
  bars.forEach((b, i) => {
    if (i > lim) return;
    const prev = i ? bars[i - 1].close : b.close;
    const h = Math.max(1.5, Math.sqrt(b.volume / vmax) * vMaxH);
    ctx.fillStyle = b.close >= prev ? "rgba(255,84,73,.26)" : "rgba(46,194,126,.24)";
    const x = xAt(i) - 2.5;
    if (ctx.roundRect) {
      ctx.beginPath();
      ctx.roundRect(x, vBase - h, 5, h, [2, 2, 0, 0]);
      ctx.fill();
    } else ctx.fillRect(x, vBase - h, 5, h);
  });

  /* 年线 + 下方区域 */
  if (ma250) {
    const my = yAt(ma250);
    if (my < vBase - vMaxH - 6) {
      ctx.fillStyle = "rgba(255,84,73,.05)";
      ctx.fillRect(0, my, W, vBase - vMaxH - 6 - my);
    }
    ctx.setLineDash([5, 6]);
    ctx.strokeStyle = "rgba(255,178,36,.5)";
    ctx.beginPath(); ctx.moveTo(0, my); ctx.lineTo(W, my); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255,178,36,.85)";
    ctx.font = "10px 'IBM Plex Mono', monospace";
    const tag = "年线 " + ma250.toFixed(2);
    ctx.fillText(tag, W - 8 - ctx.measureText(tag).width, Math.max(my - 8, 14));
  }

  /* 年线事件标记：跌破红 / 站回绿 */
  (S.evDates || []).forEach(ev => {
    const idx = bars.findIndex(b => b.date === ev.date);
    if (idx < 0 || idx > lim) return;
    const danger = /break/.test(ev.type);
    const mx = xAt(idx), myv = yAt(closes[idx]) - 12;
    ctx.fillStyle = danger ? "#ff3b30" : "#2ec27e";
    ctx.strokeStyle = danger ? "rgba(255,59,48,.35)" : "rgba(46,194,126,.35)";
    ctx.beginPath(); ctx.arc(mx, myv, 4, 0, 7); ctx.fill();
    ctx.beginPath();
    ctx.moveTo(mx, myv + 4);
    ctx.lineTo(mx, vBase - Math.sqrt(bars[idx].volume / vmax) * vMaxH);
    ctx.stroke();
  });

  /* MA5 / MA10（平滑曲线） */
  const maPts = arr => arr.map((v, i) => (i <= lim && v != null ? { x: xAt(i), y: yAt(v) } : null))
    .filter(pt => pt);
  const drawSmoothed = (pts, color, width) => {
    if (!pts || pts.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    smoothPath(ctx, pts);
    ctx.stroke();
  };
  drawSmoothed(maPts(ma10s), "rgba(177,140,255,.65)", 1);
  drawSmoothed(maPts(ma5s), "rgba(83,199,240,.7)", 1);

  /* 收盘价主线（平滑 + 柔光 + draw-in） */
  const pricePts = [];
  for (let i = 0; i <= Math.min(lim, n - 1); i++) {
    pricePts.push({ x: xAt(i), y: yAt(closes[i]) });
  }
  if (pricePts.length >= 2) {
    ctx.beginPath();
    smoothPath(ctx, pricePts);
    ctx.lineTo(pricePts[pricePts.length - 1].x, vBase);
    ctx.lineTo(pricePts[0].x, vBase);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, "rgba(234,231,224,.11)");
    grad.addColorStop(1, "rgba(234,231,224,0)");
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.save();
    ctx.shadowBlur = 12;
    ctx.shadowColor = "rgba(234,231,224,.3)";
    ctx.beginPath();
    smoothPath(ctx, pricePts);
    ctx.strokeStyle = "#eae7e0";
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.restore();
    /* 末点辉光 */
    const lp = pricePts[pricePts.length - 1];
    const glow = ctx.createRadialGradient(lp.x, lp.y, 0, lp.x, lp.y, 13);
    glow.addColorStop(0, "rgba(255,84,73,.55)");
    glow.addColorStop(1, "rgba(255,84,73,0)");
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(lp.x, lp.y, 13, 0, 7); ctx.fill();
    ctx.fillStyle = "#ff5449";
    ctx.beginPath(); ctx.arc(lp.x, lp.y, 3.2, 0, 7); ctx.fill();
  }

  /* 十字线 */
  if (cross != null) {
    const cx = xAt(cross);
    ctx.strokeStyle = "rgba(234,231,224,.35)";
    ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(cx, 10); ctx.lineTo(cx, vBase); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(cx, yAt(closes[cross]), 3.4, 0, 7); ctx.fill();
  }

  /* 日期轴 */
  ctx.fillStyle = "rgba(119,117,109,.9)";
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillText(bars[0].date.slice(5), 8, H - 1);
  const tl = bars[n - 1].date.slice(5);
  ctx.fillText(tl, W - 8 - ctx.measureText(tl).width, H - 1);
}

function attachChartInteractions(cv) {
  cv.onmousemove = e => {
    if (!chartState) return;
    const rect = cv.getBoundingClientRect();
    const idx = Math.round((e.clientX - rect.left - 8) / (rect.width - 16)
                           * (chartState.bars.length - 1));
    chartState.cross = Math.max(0, Math.min(chartState.bars.length - 1, idx));
    renderChart(1);
    const b = chartState.bars[chartState.cross];
    const prev = chartState.cross ? chartState.bars[chartState.cross - 1].close : b.close;
    const tip = document.querySelector("#chart-tip");
    if (!tip) return;
    tip.innerHTML = `<b>${b.date}</b><br>收盘 <b>${b.close.toFixed(2)}</b>` +
      ` · 涨跌 <b>${(b.close / prev * 100 - 100).toFixed(2)}%</b><br>` +
      `MA5 ${fmtNum2(chartState.ma5s ? chartState.ma5s[chartState.cross] : null)}` +
      ` · MA10 ${fmtNum2(chartState.ma10s ? chartState.ma10s[chartState.cross] : null)}` +
      `<br>量 ${fmtVol(b.volume)}`;
    tip.hidden = false;
    const wrap = tip.parentElement.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - wrap.left + 14, wrap.width - 190) + "px";
    tip.style.top = (e.clientY - wrap.top - 10) + "px";
  };
  cv.onmouseleave = () => {
    if (!chartState) return;
    chartState.cross = null;
    renderChart(1);
    const tip = document.querySelector("#chart-tip");
    if (tip) tip.hidden = true;
  };
}

/* 分时 · 实时：昨收基准线 + 相对昨收涨跌着色 + 十字线 */
let intradayState = null;
let intradayStarted = false;
async function loadIntraday(id) {
  try {
    const d = await api(`/api/instruments/${id}/intraday`);
    intradayState = { cv: $("#d-intraday"), ...d, cross: null };
    renderIntraday(1);
    attachIntradayInteractions();
    $("#d-intraday-cap").textContent =
      `${d.date} 分时${d.source.includes("proxy") ? " · SMH代理重标定" : ""}` +
      ` · 基准昨收 ${fmtPrice(d.preClose)}`;
  } catch (e) {
    $("#d-intraday-cap").textContent = "分时数据不可用：" + e.message;
  }
}
function renderIntraday(p = 1) {
  const S = intradayState;
  if (!S || !S.points.length) return;
  const cv = S.cv;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const W = S.W = cv.clientWidth || 600, H = S.H = cv.clientHeight || 220;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const prices = S.points.map(pt => pt[1]).slice(0, Math.max(2, Math.floor(S.points.length * p)));
  const values = prices.concat([S.preClose]);
  let lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo) * .12 || 1; lo -= pad; hi += pad;
  const n = S.points.length;
  const xAt = i => 8 + (W - 16) * (i / (n - 1));
  const yAt = v => 14 + (H - 44) * (1 - (v - lo) / (hi - lo));
  ctx.clearRect(0, 0, W, H);
  /* 昨收基准虚线 */
  const py = yAt(S.preClose);
  ctx.setLineDash([4, 5]);
  ctx.strokeStyle = "rgba(234,231,224,.4)";
  ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(W, py); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(234,231,224,.55)";
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillText("昨收 " + S.preClose.toFixed(2), 6, py - 5);
  /* 价格平滑曲线（逐段按相对昨收着色） */
  const last = Math.max(1, Math.floor((n - 1) * Math.min(1, p)) + (p < 1 ? 1 : 0));
  for (let i = 1; i <= last; i++) {
    const up = S.points[i][1] >= S.preClose;
    ctx.strokeStyle = up ? "rgba(255,84,73,.9)" : "rgba(46,194,126,.9)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(xAt(i - 1), yAt(S.points[i - 1][1]));
    ctx.lineTo(xAt(i), yAt(S.points[i][1]));
    ctx.stroke();
  }
  /* 渐变区域（相对昨收） */
  ctx.beginPath();
  ctx.moveTo(xAt(0), py);
  for (let i = 0; i <= last; i++) ctx.lineTo(xAt(i), yAt(S.points[i][1]));
  ctx.lineTo(xAt(last), py);
  ctx.closePath();
  const above = S.points[last][1] >= S.preClose;
  const grad = ctx.createLinearGradient(0, Math.min(py, yAt(S.points[last][1])), 0, Math.max(py, yAt(S.points[last][1])));
  grad.addColorStop(0, above ? "rgba(255,84,73,.16)" : "rgba(46,194,126,.16)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = grad;
  ctx.fill();
  /* 末点辉光 */
  const lp = { x: xAt(last), y: yAt(S.points[last][1]) };
  const glow = ctx.createRadialGradient(lp.x, lp.y, 0, lp.x, lp.y, 11);
  glow.addColorStop(0, above ? "rgba(255,84,73,.55)" : "rgba(46,194,126,.55)");
  glow.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = glow;
  ctx.beginPath(); ctx.arc(lp.x, lp.y, 11, 0, 7); ctx.fill();
  ctx.fillStyle = above ? "#ff5449" : "#2ec27e";
  ctx.beginPath(); ctx.arc(lp.x, lp.y, 3, 0, 7); ctx.fill();
  /* 时间轴 */
  ctx.fillStyle = "rgba(119,117,109,.9)";
  ctx.font = "10px 'IBM Plex Mono', monospace";
  ctx.fillText(S.points[0][0], 6, H - 1);
  const tm = S.points[n - 1][0];
  ctx.fillText(tm, W - 6 - ctx.measureText(tm).width, H - 1);
}
function attachIntradayInteractions() {
  const cv = intradayState.cv;
  cv.onmousemove = e => {
    if (!intradayState) return;
    const rect = cv.getBoundingClientRect();
    const idx = Math.round((e.clientX - rect.left - 8) / (rect.width - 16)
                           * (intradayState.points.length - 1));
    intradayState.cross = Math.max(0, Math.min(intradayState.points.length - 1, idx));
    renderIntraday(1);
    const [tm, pr] = intradayState.points[intradayState.cross];
    const chg = intradayState.preClose ? (pr / intradayState.preClose * 100 - 100) : null;
    const tip = document.querySelector("#intraday-tip");
    if (!tip) return;
    tip.innerHTML = `<b>${tm}</b><br>价格 <b>${pr.toFixed(2)}</b>` +
      (chg != null ? ` · 相对昨收 <b>${(chg > 0 ? "+" : "") + chg.toFixed(2)}%</b>` : "");
    tip.hidden = false;
    const wrap = tip.parentElement.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - wrap.left + 14, wrap.width - 190) + "px";
    tip.style.top = (e.clientY - wrap.top - 10) + "px";
  };
  cv.onmouseleave = () => {
    if (!intradayState) return;
    intradayState.cross = null;
    renderIntraday(1);
    const tip = document.querySelector("#intraday-tip");
    if (tip) tip.hidden = true;
  };
}
function startIntraday() {
  if (intradayStarted) return;
  intradayStarted = true;
  setInterval(() => {
    if (document.hidden || !me || currentNav !== "detail" || !detailItem) return;
    if (!marketPulse()) return;   // 非交易时段静态显示最近交易日
    loadIntraday(detailItem.id);
  }, Math.max(30, (me && me.intraday_refresh_secs) || 60) * 1000);
}

function fmtVol(v) {
  if (v == null || isNaN(v) || v <= 0) return "--";
  if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (v >= 1e4) return (v / 1e4).toFixed(1) + "万";
  return String(Math.round(v));
}

/* ---------------- admin ---------------- */
async function loadAdmin() {
  const data = await api("/api/admin/users");
  const wrap = $("#admin-list");
  const users = data.users || [];
  wrap.innerHTML = users.map(u => {
    const pending = !u.approved;
    const isAdmin = u.role === "admin";
    const isSelf = me && u.id === me.id;
    const roleBtn = (!isSelf && !pending) ? `
      <button class="btn-magnetic sm role-btn" type="button"
              title="${isAdmin ? "降为普通用户" : "提升为管理员"}">
        <span class="ic">${iconSvg(isAdmin ? "arrow-down-right" : "arrow-up-right")}</span>
        <span>${isAdmin ? "降为用户" : "提升管理员"}</span>
      </button>` : `<span></span>`;
    return `
    <div class="wl-row" data-uid="${u.id}">
      <div class="wl-id">
        <span class="item-name">${esc(u.username)}${isSelf ? "（我）" : ""}</span>
        <span class="item-code">${isAdmin ? "管理员" : "用户"} · ${fmtTime(u.created_at)}</span>
      </div>
      <div class="wl-ma"><small>状态</small>${pending
        ? `<span class="up">待审核</span>` : `已启用`}</div>
      <span class="badge ${pending ? "badge-pending" : "badge-above"} wl-status">
        <span class="dot"></span>${pending ? "待审核" : (isAdmin ? "管理员" : "正常")}
      </span>
      ${pending ? `<button class="btn-magnetic sm approve-btn" type="button">
        <span class="ic">${iconSvg("check")}</span><span>通过</span></button>` : roleBtn}
      <button class="del-btn" title="${isAdmin && !isSelf ? "管理员账号不可删除" : "删除用户"}"
              ${isAdmin ? "disabled" : ""}>
        <span class="ic">${iconSvg("trash-2")}</span></button>
    </div>`;
  }).join("") || `<div class="empty-state">没有用户</div>`;

  $$(".wl-row", wrap).forEach(row => {
    const id = row.dataset.uid;
    const approve = $(".approve-btn", row);
    if (approve) approve.addEventListener("click", async () => {
      try {
        await api(`/api/admin/users/${id}/approve`, { method: "POST" });
        toast("已通过");
        loadAdmin();
      } catch (e) { toast(e.message, true); }
    });
    const roleBtn = $(".role-btn", row);
    if (roleBtn) roleBtn.addEventListener("click", async () => {
      const u = users.find(x => x.id + "" === id + "");
      const target = u && u.role === "admin" ? "user" : "admin";
      try {
        await api(`/api/admin/users/${id}/set-role`,
                  { method: "POST", body: { role: target } });
        toast(target === "admin" ? "已提升为管理员" : "已降为普通用户");
        loadAdmin();
      } catch (e) { toast(e.message, true); }
    });
    const del = $(".del-btn", row);
    if (del && !del.disabled) del.addEventListener("click", async () => {
      if (!del.classList.contains("arm")) {
        del.classList.add("arm");
        del.innerHTML = `<span class="ic">${iconSvg("check")}</span>`;
        setTimeout(() => {
          del.classList.remove("arm");
          del.innerHTML = `<span class="ic">${iconSvg("trash-2")}</span>`;
        }, 2600);
        return;
      }
      try {
        await api(`/api/admin/users/${id}`, { method: "DELETE" });
        toast("用户已删除");
        loadAdmin();
      } catch (e) { toast(e.message, true); }
    });
  });
}

/* ---------------- admin mail settings ---------------- */
async function loadMailSettings() {
  const msg = $("#mail-msg");
  msg.hidden = true;
  try {
    const d = await api("/api/admin/mail");
    const f = $("#mail-form");
    f.host.value = d.host || "";
    f.port.value = d.port || "465";
    f.user.value = d.user || "";
    f.mail_from.value = d.mail_from || "";
    f.tls.value = d.tls || "ssl";
    f.passwd.placeholder = d.has_pass ? "已保存（留空保持不变）" : "授权码 / 密码";
    f.mail_from.placeholder = d.mail_from || "默认同账号";
    msg.textContent = d.configured ? "邮件通知已启用" : "尚未完整配置——填写后保存即可启用";
    msg.classList.toggle("info", !!d.configured);
    msg.hidden = false;
  } catch (e) { msg.textContent = e.message; msg.hidden = false; }
}
function initMailSettings() {
  $("#mail-save").addEventListener("click", async () => {
    const f = $("#mail-form");
    const msg = $("#mail-msg");
    msg.classList.remove("info"); msg.hidden = true;
    try {
      await api("/api/admin/mail", { method: "POST", body: {
        host: f.host.value.trim(), port: f.port.value.trim(),
        user: f.user.value.trim(), passwd: f.passwd.value,
        mail_from: f.mail_from.value.trim(), tls: f.tls.value,
      }});
      msg.textContent = "SMTP 配置已保存";
      msg.classList.add("info"); msg.hidden = false;
      loadMailSettings();
      toast("SMTP 配置已保存");
    } catch (e) { msg.textContent = e.message; msg.hidden = false; }
  });
  $("#mail-test").addEventListener("click", async () => {
    const msg = $("#mail-msg");
    msg.classList.remove("info"); msg.hidden = true;
    try {
      await api("/api/admin/mail/test", { method: "POST" });
      msg.textContent = "测试邮件已发送——查收你的通知邮箱确认";
      msg.classList.add("info"); msg.hidden = false;
    } catch (e) { msg.textContent = e.message; msg.hidden = false; }
  });
}

/* ---------------- logs ---------------- */
async function loadRuns() {
  const data = await api("/api/runs?limit=80");
  const term = $("#term");
  const rows = data.runs || [];
  if (!rows.length) {
    term.innerHTML = `<div class="term-empty">暂无运行记录 —— 交易窗口内每 5 分钟一条</div>`;
    return;
  }
  term.innerHTML = rows.map(r => `
    <div class="term-line">
      <span class="term-ts">${fmtTime(r.ts)}</span>
      <span class="term-win">${esc(r.window || "")}</span>
      <span class="term-dec ${esc(r.decision)}">${esc((r.decision || "").toUpperCase())}</span>
      <span class="term-reason">${esc(r.reason || "")}</span>
    </div>`).join("");
}

/* ---------------- routing ---------------- */
let currentNav = "dashboard";
function nav(name) {
  if (name === currentNav) return;
  if (name === "admin" && (!me || me.role !== "admin")) return;
  if (name !== "detail") { detailSeries = null; backdropMode = backdropSeries && backdropSeries.length ? "dash" : "ambient"; }
  if (name !== "detail") prevNav = currentNav === "detail" ? prevNav : currentNav;
  currentNav = name;
  const from = $(`.panel.is-active`);
  const to = $(`#panel-${name}`);
  switchView(from, to);
  $$(".rail-item, .tab").forEach(b => b.classList.toggle("is-active", b.dataset.nav === name));
  if (name === "dashboard") { loadDashboard(); loadEvents(); }
  if (name === "watchlist") loadWatchlist();
  if (name === "logs") loadRuns();
  if (name === "admin") { loadAdmin(); loadMailSettings(); }
  history.replaceState(null, "", "#/" + name);
}
function openDetail(id) {
  if (currentNav !== "detail") prevNav = currentNav;
  currentNav = "detail";
  const from = $(".panel.is-active");
  const to = $("#panel-detail");
  if (from !== to) switchView(from, to);
  $$(".rail-item, .tab").forEach(b => b.classList.remove("is-active"));
  history.replaceState(null, "", "#/detail/" + id);
  loadDetail(id);
}
function initNav() {
  $$("[data-nav]").forEach(b => b.addEventListener("click", () => nav(b.dataset.nav)));
  const h = (location.hash || "").replace("#/", "");
  if (["dashboard", "watchlist", "logs", "guide"].includes(h)) nav(h);
}

/* ---------------- change password ---------------- */
function initPassword() {
  const dlg = $("#pw-dialog");
  const errEl = $("#pw-error");
  $("#btn-password").addEventListener("click", () => {
    $("#pw-form").reset();
    $("#email-form").reset();
    errEl.hidden = true;
    if (me && me.email) $('input[name="notify_email"]').value = me.email;
    dlg.hidden = false;
  });
  $("#email-save").addEventListener("click", async () => {
    const em = $('input[name="notify_email"]').value.trim();
    const msg = $("#email-msg");
    msg.classList.remove("info"); msg.hidden = true;
    try {
      const r = await api("/api/me/email", { method: "POST", body: { email: em } });
      me.email = r.email;
      msg.textContent = r.email ? "通知邮箱已保存" : "通知邮箱已清除";
      msg.classList.add("info"); msg.hidden = false;
    } catch (err) {
      msg.textContent = err.message; msg.hidden = false;
    }
  });
  $("#email-test").addEventListener("click", async () => {
    const msg = $("#email-msg");
    msg.classList.remove("info"); msg.hidden = true;
    try {
      await api("/api/test-email", { method: "POST" });
      msg.textContent = "测试邮件已发送——查收邮箱确认";
      msg.classList.add("info"); msg.hidden = false;
    } catch (err) {
      msg.textContent = err.message; msg.hidden = false;
    }
  });
  $("#pw-close").addEventListener("click", () => { dlg.hidden = true; });
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.hidden = true; });
  $("#pw-form").addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const nw = fd.get("new_password") || "";
    if (nw !== (fd.get("confirm") || "")) {
      errEl.textContent = "两次输入的新密码不一致";
      errEl.classList.remove("info");
      errEl.hidden = false;
      return;
    }
    try {
      await api("/api/change-password", {
        method: "POST",
        body: { old_password: fd.get("old_password") || "", new_password: nw },
      });
      dlg.hidden = true;
      me = null;
      showAuth();
      toast("密码已修改，请用新密码登录");
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("info");
      errEl.hidden = false;
    }
  });
}

/* ---------------- boot ---------------- */
async function boot() {
  try {
    me = await api("/api/me");
  } catch (e) {
    showAuth();
    return;
  }
  showApp();
  const dm = (location.hash || "").match(/^#\/detail\/(\d+)/);
  const h = (location.hash || "").replace("#/", "");
  const isAdmin = me && me.role === "admin";
  $$(".admin-only").forEach(el => { el.hidden = !isAdmin; });
  const valid = ["dashboard", "watchlist", "logs", "guide", ...(isAdmin ? ["admin"] : [])];
  currentNav = valid.includes(h) ? h : "dashboard";
  $$(".panel").forEach(p => p.classList.remove("is-active"));
  $(`#panel-${dm ? "detail" : currentNav}`).classList.add("is-active");
  $$(".rail-item, .tab").forEach(b => b.classList.toggle("is-active", b.dataset.nav === currentNav));
  if (currentNav === "dashboard" && !dm) renderSkeleton();
  startIntraday();
  await Promise.all([loadDashboard(), loadEvents()]).catch(e => toast(e.message, true));
  if (currentNav === "watchlist") loadWatchlist();
  if (currentNav === "logs") loadRuns();
  if (currentNav === "admin") loadAdmin();
  if (currentNav === "admin") loadMailSettings();
  startQuotes();
  if (dm) openDetail(dm[1]);
}

function initChrome() {
  $("#dash-groups").addEventListener("click", e => {
    const item = e.target.closest(".item");
    if (item && item.dataset.iid) openDetail(item.dataset.iid);
  });
  $$("#dash-filter .seg-btn").forEach(b => b.addEventListener("click", () => {
    const f = b.dataset.filter;
    if (f === "all") { dashSel = null; applyDashFilter(); return; }
    if (dashSel === null) { dashSel = new Set([f]); applyDashFilter(); return; }
    if (dashSel.has(f)) {
      dashSel.delete(f);
      dashSel = dashSel.size ? dashSel : null;   // 全取消 = 全部
    } else {
      dashSel.add(f);
    }
    applyDashFilter();
  }));
  $("#detail-back").addEventListener("click", () => nav(prevNav || "dashboard"));
  addEventListener("resize", () => {
    if (currentNav !== "detail" || !chartState) return;
    const cv = chartState.cv;
    chartState.dpr = Math.min(devicePixelRatio || 1, 2);
    chartState.W = cv.clientWidth || 600;
    chartState.H = cv.clientHeight || 240;
    cv.width = chartState.W * chartState.dpr;
    cv.height = chartState.H * chartState.dpr;
    renderChart(chartState._p == null ? 1 : chartState._p);
  });
  $("#btn-logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    me = null;
    $$(".admin-only").forEach(el => { el.hidden = true; });
    if (currentNav === "admin") currentNav = "dashboard";
    showAuth();
  });
  $("#btn-refresh").addEventListener("click", async () => {
    const btn = $("#btn-refresh");
    btn.classList.add("busy");
    try {
      await refreshCurrent();
    } finally {
      setTimeout(() => btn.classList.remove("busy"), 450);
    }
  });
  setInterval(() => {
    if (!document.hidden && me) refreshCurrent().catch(() => {});
  }, 60000);
}
async function refreshCurrent() {
  if (currentNav === "dashboard") await Promise.all([loadDashboard(), loadEvents()]);
  else if (currentNav === "watchlist") await loadWatchlist();
  else if (currentNav === "admin") await loadAdmin();
  else if (currentNav === "guide") { /* 静态说明页，无需刷新 */ }
  else await loadRuns();
}

/* run-once (logs view) */
function initRunOnce() {
  const head = $("#panel-logs .sec-head");
  const btn = document.createElement("button");
  btn.className = "btn-magnetic sm";
  btn.innerHTML = `<span class="ic">${iconSvg("arrow-right")}</span><span>立即检测</span>`;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    toast("检测运行中……");
    try {
      const r = await api("/api/run-once", { method: "POST" });
      toast(r.status === "ok" ? `检测完成：${r.decision === "wake" ? "有唤醒事件" : "静默"} · ${r.reason || ""}`
        : r.status === "skipped" ? "当前不在交易窗口（已强制运行）" : `错误：${r.reason}`);
      loadRuns();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });
  head.appendChild(btn);
}

/* ---------------- start ---------------- */
hydrateIcons(document);
startBackdrop();
initAuth();
initNav();
initAddForm();
initChrome();
initRunOnce();
initMailSettings();
initPassword();
renderChips();
magnetic(".btn-magnetic");
positionTabInk();
if (document.fonts && document.fonts.ready) document.fonts.ready.then(positionTabInk);
boot();
