/* ============================================================
   Autonomous Trading System — dashboard client
   Vanilla JS, zero dependencies. Canvas charts are hand-rolled
   so the dashboard works fully offline.
   ============================================================ */
"use strict";

/* ---------------------------------------------------------- utilities */
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const fmtMoney = (n) =>
  n == null || !isFinite(n) ? "—"
  : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

const fmtMoney2 = (n) =>
  n == null || !isFinite(n) ? "—"
  : n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 });

const fmtPct = (n, d = 2) =>
  n == null || !isFinite(n) ? "—"
  : (n * 100).toFixed(d) + "%";

const fmtNum = (n, d = 2) =>
  n == null || !isFinite(n) ? "—" : Number(n).toFixed(d);

const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "neu");
const moneyClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "neu");

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const shortTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toISOString().slice(0, 16).replace("T", " ");
};

async function api(path, options) {
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...(options || {}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.warn("api", path, err);
    return null;
  }
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast-wrap").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s, transform .3s";
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    setTimeout(() => el.remove(), 320);
  }, 2800);
}

/* ---------------------------------------------------------- chart core */
class Chart {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.series = [];
    this.cfg = {};
    this.resize();
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const box = this.canvas.parentElement.getBoundingClientRect();
    this.w = Math.max(80, box.width - 12);
    this.h = Math.max(60, box.height - 12);
    this.canvas.width = this.w * dpr;
    this.canvas.height = this.h * dpr;
    this.canvas.style.width = this.w + "px";
    this.canvas.style.height = this.h + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  }

  /** Set data and draw. series: [{color, data:[y,...], fill, labels, width, dash}] */
  set(series, cfg = {}) {
    this.series = series || [];
    this.cfg = cfg;
    this.draw();
  }

  draw() {
    const { ctx, w, h } = this;
    ctx.clearRect(0, 0, w, h);
    const pad = { l: 8, r: 62, t: 12, b: 20 };
    const plotW = w - pad.l - pad.r;
    const plotH = h - pad.t - pad.b;

    // grid
    ctx.strokeStyle = "rgba(29,42,55,.85)";
    ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";

    const visible = this.series.filter((s) => s.data && s.data.length);
    if (!visible.length) {
      ctx.fillStyle = "#526475";
      ctx.textAlign = "center";
      ctx.fillText("no data", w / 2, h / 2);
      return;
    }

    let lo = Infinity, hi = -Infinity;
    for (const s of visible) {
      for (const v of s.data) {
        if (v == null || !isFinite(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (this.cfg.includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (this.cfg.minY != null) lo = Math.min(lo, this.cfg.minY);
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (hi === lo) { hi += 1; lo -= 1; }
    const padY = (hi - lo) * 0.08;
    lo -= padY; hi += padY;

    const maxLen = Math.max(...visible.map((s) => s.data.length));
    const X = (i) => pad.l + (maxLen <= 1 ? plotW / 2 : (i / (maxLen - 1)) * plotW);
    const Y = (v) => pad.t + plotH - ((v - lo) / (hi - lo)) * plotH;

    // horizontal grid + right axis labels
    ctx.textAlign = "left";
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      const y = Math.round(Y(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();
      ctx.fillStyle = "#526475";
      let label;
      if (Math.abs(v) >= 1000 || this.cfg.money) label = "$" + Math.round(v).toLocaleString();
      else if (this.cfg.pct) label = (v * 100).toFixed(0) + "%";
      else label = v.toFixed(2);
      ctx.fillText(label, pad.l + plotW + 6, y);
    }

    // zero line
    if (lo < 0 && hi > 0) {
      ctx.strokeStyle = "rgba(124,143,163,.45)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(pad.l, Y(0));
      ctx.lineTo(pad.l + plotW, Y(0));
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // series
    for (const s of visible) {
      const pts = s.data
        .map((v, i) => (v == null || !isFinite(v) ? null : [X(i), Y(v)]))
        .filter(Boolean);

      if (s.fill && pts.length > 1) {
        const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
        grad.addColorStop(0, s.color + "55");
        grad.addColorStop(1, s.color + "00");
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pad.t + plotH);
        pts.forEach(([x, y]) => ctx.lineTo(x, y));
        ctx.lineTo(pts[pts.length - 1][0], pad.t + plotH);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();
      }

      ctx.beginPath();
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 1.7;
      ctx.setLineDash(s.dash || []);
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // x labels (first / middle / last)
    const labels = this.cfg.labels || [];
    ctx.fillStyle = "#526475";
    ctx.textAlign = "left";
    if (labels.length) {
      const idx = [0, Math.floor(labels.length / 2), labels.length - 1];
      idx.forEach((i, k) => {
        if (!labels[i]) return;
        ctx.textAlign = k === 0 ? "left" : k === 2 ? "right" : "center";
        ctx.fillText(labels[i], X(i), h - 7);
      });
    }

    // last value marker
    const last = visible[visible.length - 1];
    if (last && last.data.length) {
      for (let i = last.data.length - 1; i >= 0; i--) {
        const v = last.data[i];
        if (v != null && isFinite(v)) {
          const y = Y(v);
          ctx.fillStyle = last.color;
          ctx.beginPath();
          ctx.arc(X(i), y, 3.2, 0, Math.PI * 2);
          ctx.fill();
          ctx.beginPath();
          ctx.moveTo(X(i), pad.t);
          ctx.lineTo(X(i), pad.t + plotH);
          ctx.strokeStyle = last.color + "33";
          ctx.lineWidth = 1;
          ctx.stroke();
          break;
        }
      }
    }
  }
}

/** Histogram chart for R-multiples. */
function drawHistogram(canvas, values, opts = {}) {
  const chart = new Chart(canvas);
  if (!values || !values.length) { chart.set([], {}); return; }
  const min = Math.min(...values), max = Math.max(...values);
  const bins = opts.bins || 19;
  const span = Math.max(max - min, 1e-6);
  const width = span / bins;
  const counts = new Array(bins).fill(0);
  for (const v of values) {
    let b = Math.floor((v - min) / width);
    if (b < 0) b = 0;
    if (b >= bins) b = bins - 1;
    counts[b]++;
  }
  const colors = counts.map((_, i) => {
    const center = min + (i + 0.5) * width;
    return center >= 0 ? "#34d399" : "#f87171";
  });
  chart.set(
    [{ color: "#38bdf8", data: counts, fill: false, width: 0 }],
    { includeZero: true }
  );
  // redraw manually as bars
  const ctx = chart.ctx, w = chart.w, h = chart.h;
  const pad = { l: 8, r: 44, t: 12, b: 20 };
  const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
  const maxCount = Math.max(...counts, 1);

  const chart2 = chart;
  chart2.cfg = {};
  const redraw = () => {
    const c = chart2.ctx;
    c.clearRect(0, 0, w, h);
    c.font = "10px ui-monospace, monospace";
    c.textBaseline = "middle";
    const bw = plotW / bins;
    for (let k = 0; k <= 4; k++) {
      const y = pad.t + (plotH * k) / 4;
      c.strokeStyle = "rgba(29,42,55,.85)";
      c.beginPath(); c.moveTo(pad.l, y); c.lineTo(pad.l + plotW, y); c.stroke();
      c.fillStyle = "#526475"; c.textAlign = "left";
      c.fillText(Math.round(maxCount * (1 - k / 4)), pad.l + plotW + 6, y);
    }
    // zero line
    const zeroBin = Math.floor((0 - min) / width);
    if (zeroBin >= 0 && zeroBin <= bins) {
      const zx = pad.l + zeroBin * bw;
      c.strokeStyle = "rgba(251,191,36,.55)";
      c.setLineDash([3, 3]);
      c.beginPath(); c.moveTo(zx, pad.t); c.lineTo(zx, pad.t + plotH); c.stroke();
      c.setLineDash([]);
    }
    counts.forEach((n, i) => {
      if (!n) return;
      const bh = (n / maxCount) * plotH;
      const x = pad.l + i * bw + 1;
      const y = pad.t + plotH - bh;
      c.fillStyle = colors[i];
      c.globalAlpha = 0.85;
      c.fillRect(x, y, Math.max(1, bw - 2), bh);
      c.globalAlpha = 1;
    });
    c.fillStyle = "#526475";
    c.textAlign = "left";  c.fillText(min.toFixed(1) + "R", pad.l, h - 7);
    c.textAlign = "right"; c.fillText(max.toFixed(1) + "R", pad.l + plotW, h - 7);
  };
  redraw();
  chart.resolution = redraw;
  return chart;
}

/* ---------------------------------------------------------- app state */
const charts = {
  equity: new Chart($("#chart-equity")),
  dd: new Chart($("#chart-dd")),
  hist: null,
  btEquity: new Chart($("#chart-bt-equity")),
};

let tradeFilter = "all";
let cachedTrades = [];
let cachedBacktest = {};

/* ---------------------------------------------------------- rendering */
function kpiTile(label, value, sub, cls = "neu", accent = "#1d2a37", spark = null) {
  return `<div class="kpi" style="--accent:${accent}">
      <div class="k-label">${esc(label)}</div>
      <div class="k-value ${cls}">${value}</div>
      ${sub ? `<div class="k-sub">${esc(sub)}</div>` : ""}
      ${spark && spark.length > 1 ? `<canvas class="spark" data-spark="${spark.join(",")}"
         data-color="${sparkColor(cls)}"></canvas>` : ""}
    </div>`;
}

function sparkColor(cls) {
  return { pos: "#34d399", neg: "#f87171", warn: "#fbbf24" }[cls] || "#38bdf8";
}

/** Render every sparkline on the page from its data attributes. */
function drawAllSparklines() {
  document.querySelectorAll("canvas.spark[data-spark]").forEach((cv) => {
    const values = (cv.dataset.spark || "").split(",").map(Number).filter((v) => isFinite(v));
    drawSparkline(cv, values.slice(-60), cv.dataset.color || "#38bdf8");
  });
}

/** Compose the hero strip used at the top of the Overview. */
function heroStrip(snap, metrics, equitySeries) {
  const equity = snap.equity;
  const bt = (metrics && metrics.backtest && metrics.backtest.metrics) || {};
  const series = (equitySeries || []).map((r) => r.equity).filter((v) => v != null);
  const last = series.length ? series[series.length - 1] : null;
  const prev = series.length > 1 ? series[series.length - 2] : null;
  const delta = (last != null && prev != null) ? last - prev : null;
  const mode = (snap.mode || "IDLE").toUpperCase();
  const dayCls = moneyClass(snap.day_pnl || 0);

  const sides = [
    { l: "Day P&L", v: fmtMoney2(snap.day_pnl), c: dayCls },
    { l: "Open risk", v: fmtMoney(snap.risk ? snap.risk.open_risk : null), c: "neu" },
    { l: "Drawdown", v: fmtPct(bt.max_drawdown), c: "neg" },
    { l: "Expectancy", v: (bt.expectancy_R != null ? fmtNum(bt.expectancy_R, 2) + "R" : "—"),
      c: (bt.expectancy_R || 0) > 0 ? "pos" : "neg" },
    { l: "Win rate", v: fmtPct(bt.win_rate), c: "neu" },
    { l: "Positions", v: (snap.positions || []).length + " open", c: "neu" },
  ];

  return `<div class="hero-strip">
    <div class="hero-main">
      <div class="hero-sub">EQUITY · ${esc(mode)}</div>
      <div class="hero-equity">${fmtMoney(equity)}</div>
      <div class="hero-delta ${delta >= 0 ? "pos" : "neg"}">
        ${delta == null ? "" : (delta >= 0 ? "▲ " : "▼ ") + fmtMoney2(Math.abs(delta))}
      </div>
      <div class="hero-sub">${series.length} recorded bars · last backtest CAGR ${fmtPct(bt.cagr)}</div>
    </div>
    <div class="hero-side">
      ${sides.map((s) => `<div class="mini-card">
          <span class="m-label">${esc(s.l)}</span>
          <span class="m-value ${s.c}">${s.v}</span>
        </div>`).join("")}
    </div>
  </div>
  <div class="kpi-grid">`;
}

function renderOverview(snap, metrics) {
  const bt = (metrics && metrics.backtest && metrics.backtest.metrics) || {};
  const live = (metrics && metrics.live) || {};
  const equity = snap.equity ?? live.start_equity ?? null;
  const dayRet = snap.day_return_pct;

  const eqSeries = (snap.equity_series || []).filter((r) => r && r.equity != null);
  const eqValues = eqSeries.map((r) => r.equity);

  const tiles = [
    kpiTile("Total Return", fmtPct(bt.total_return ?? snap.total_return_pct ?? null),
            `CAGR ${fmtPct(bt.cagr ?? null)}`,
            moneyClass((bt.total_return ?? snap.total_return_pct) ?? 0), "#a78bfa",
            eqValues),
    kpiTile("Day P&L", fmtMoney2(snap.day_pnl), fmtPct(dayRet ?? null),
            moneyClass(snap.day_pnl ?? 0), "#38bdf8"),
    kpiTile("Max Drawdown", fmtPct(bt.max_drawdown ?? null), "peak-to-trough",
            "neg", "#f87171"),
    kpiTile("Sharpe", fmtNum(bt.sharpe), `Sortino ${fmtNum(bt.sortino)}`, "neu", "#fbbf24"),
    kpiTile("Expectancy", fmtNum(bt.expectancy_R, 2) + "R",
            `${fmtPct(bt.expectancy_pct, 3)} / trade`, (bt.expectancy_R ?? 0) > 0 ? "pos" : "neg",
            (bt.expectancy_R ?? 0) > 0 ? "#34d399" : "#f87171"),
    kpiTile("Win Rate", fmtPct(bt.win_rate ?? null), `${bt.n_trades ?? 0} trades`,
            "neu", "#38bdf8"),
    kpiTile("Profit Factor", fmtNum(bt.profit_factor),
            `max consec. losses ${bt.max_consecutive_losses ?? "—"}`,
            (bt.profit_factor ?? 0) > 1 ? "pos" : "neg", "#34d399"),
  ];
  const heroHtml = heroStrip(snap, metrics, eqSeries);
  const grid = $("#kpi-grid");
  grid.className = "hero-tiles";
  grid.innerHTML = heroHtml + tiles.join("") + "</div>";
  drawAllSparklines();

  // equity chart
  const series = (snap.equity_series || []).filter((r) => r && r.equity != null);
  if (series.length > 1) {
    const data = series.map((r) => r.equity);
    const labels = series.map((r) => shortTime(r.t).slice(0, 10));
    const start = data[0];
    charts.equity.set(
      [
        { color: "#34d399", data, fill: true, width: 1.8 },
        { color: "#526475", data: data.map(() => start), width: 1, dash: [4, 4] },
      ],
      { money: true, labels }
    );
    const peak = [], ddArr = [];
    let max = -Infinity;
    for (const v of data) {
      max = Math.max(max, v);
      ddArr.push(max > 0 ? (v - max) / max : 0);
    }
    charts.dd.set([{ color: "#f87171", data: ddArr, fill: true, width: 1.5 }],
      { pct: true, includeZero: true, labels });
  } else {
    charts.equity.set([], {});
    charts.dd.set([], {});
  }

  // R histogram from live trades
  charts.hist = drawHistogram($("#chart-hist"),
    cachedTrades.map((t) => Number(t.r_multiple)).filter((v) => isFinite(v)), { bins: 19 });

  $("#strategy-desc").textContent =
    "TREND BREAKOUT SYSTEM — 1% risk per trade\n" +
    "  Entry   : close >= 20-day high, above 200-day SMA, ADX >= 20, volume above 20d avg\n" +
    "  Stop    : entry - 2 x ATR(14)\n" +
    "  Trailing: chandelier, 3 x ATR, ratchets upward only\n" +
    "  Exit    : close below 10-day low, or trailing stop hit\n" +
    "  Sizing  : quantity = (equity x 1%) / (2 x ATR)\n" +
    "  Caps    : 8 positions, 6% total risk, -3% daily stop, -15% drawdown halt";
}

function renderPositions(positions) {
  const tbody = $("#table-positions tbody");
  $("#badge-positions").textContent = positions.length;
  if (!positions.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty">No open positions</td></tr>`;
    return;
  }
  tbody.innerHTML = positions.map((p) => {
    const dir = p.side === "long" ? 1 : -1;
    const rOpen = p.r_open != null ? Number(p.r_open)
      : p.risk_dollars ? (dir * (p.last - p.entry_price) * p.quantity) / p.risk_dollars : null;
    return `<tr>
      <td><b>${esc(p.symbol)}</b></td>
      <td><span class="side-tag side-${esc(p.side)}">${esc(p.side).toUpperCase()}</span></td>
      <td class="num">${esc(p.quantity)}</td>
      <td class="num">${fmtMoney2(p.entry_price)}</td>
      <td class="num">${fmtMoney2(p.last)}</td>
      <td class="num">${fmtMoney2(p.stop_price)}</td>
      <td class="num ${signClass(rOpen)}">${fmtNum(rOpen, 2)}R</td>
      <td class="num ${moneyClass(p.unrealized_pnl)}">${fmtMoney2(p.unrealized_pnl)}</td>
      <td class="num">${fmtMoney(p.risk_dollars)}</td>
      <td class="num">${esc(p.bars_held ?? "—")} bars</td>
    </tr>`;
  }).join("");
}

function renderTrades(trades) {
  cachedTrades = trades;
  $("#badge-trades").textContent = trades.length;
  const rows = trades
    .slice()
    .reverse()
    .filter((t) =>
      tradeFilter === "all" ? true
      : tradeFilter === "win" ? Number(t.net_pnl) > 0
      : Number(t.net_pnl) <= 0);

  const tbody = $("#table-trades tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="empty">No trades recorded</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.slice(0, 400).map((t) => `<tr>
      <td><b>${esc(t.symbol)}</b></td>
      <td><span class="side-tag side-${esc(t.side)}">${esc(t.side).toUpperCase()}</span></td>
      <td class="num">${esc(t.quantity)}</td>
      <td class="num">${shortTime(t.entry_time)}</td>
      <td class="num">${fmtMoney2(t.entry_price)}</td>
      <td class="num">${shortTime(t.exit_time)}</td>
      <td class="num">${fmtMoney2(t.exit_price)}</td>
      <td class="num ${signClass(Number(t.r_multiple))}">${fmtNum(t.r_multiple, 2)}R</td>
      <td class="num ${moneyClass(Number(t.net_pnl))}">${fmtMoney2(t.net_pnl)}</td>
      <td><span class="reason ${esc(t.exit_reason)}">${esc(t.exit_reason)}</span></td>
      <td class="num">${esc(t.bars_held ?? "—")}</td>
    </tr>`).join("");
}

function renderBacktest(bt) {
  cachedBacktest = bt || {};
  if (!bt || !Object.keys(bt).length) {
    $("#bt-kpis").innerHTML = kpiTile("Backtest", "—", "run one to populate");
    $("#mc-list").innerHTML = "";
    return;
  }
  const m = bt.metrics || {};
  $("#bt-saved").textContent = bt.saved_at ? "saved " + shortTime(bt.saved_at) : "";

  $("#bt-kpis").innerHTML = [
    kpiTile("Total Return", fmtPct(m.total_return), `CAGR ${fmtPct(m.cagr)}`,
            moneyClass(m.total_return), "#34d399"),
    kpiTile("Max Drawdown", fmtPct(m.max_drawdown), "peak-to-trough", "neg", "#f87171"),
    kpiTile("Sharpe", fmtNum(m.sharpe), `Sortino ${fmtNum(m.sortino)}`, "neu", "#fbbf24"),
    kpiTile("Expectancy", fmtNum(m.expectancy_R, 2) + "R",
            `${fmtPct(m.expectancy_pct, 3)} / trade`,
            m.expectancy_R > 0 ? "pos" : "neg", m.expectancy_R > 0 ? "#34d399" : "#f87171"),
    kpiTile("Win Rate", fmtPct(m.win_rate), `${m.n_trades} trades`, "neu", "#38bdf8"),
    kpiTile("Profit Factor", fmtNum(m.profit_factor), `payoff ${fmtNum(m.payoff)}`,
            m.profit_factor > 1 ? "pos" : "neg", "#a78bfa"),
  ].join("");

  const eq = bt.equity || [];
  if (eq.length > 1) {
    const data = eq.map((r) => r.equity);
    charts.btEquity.set(
      [{ color: "#38bdf8", data, fill: true, width: 1.9 }],
      { money: true, labels: eq.map((r) => shortTime(r.t).slice(0, 10)) }
    );
  }

  const per = bt.per_symbol || {};
  const rows = Object.entries(per).sort((a, b) => b[1].pnl - a[1].pnl);
  $("#table-symbols tbody").innerHTML = rows.length
    ? rows.map(([sym, s]) => `<tr>
        <td><b>${esc(sym)}</b></td>
        <td class="num">${s.trades}</td>
        <td class="num">${fmtPct(s.win_rate)}</td>
        <td class="num ${signClass(s.total_R)}">${fmtNum(s.total_R, 1)}R</td>
        <td class="num ${moneyClass(s.pnl)}">${fmtMoney2(s.pnl)}</td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="empty">—</td></tr>`;

  const mc = bt.monte_carlo || {};
  $("#mc-list").innerHTML = Object.keys(mc).length
    ? [
        ["P5 ending equity", mc.p5_ending + "x"],
        ["Median ending equity", mc.p50_ending + "x"],
        ["P95 ending equity", mc.p95_ending + "x"],
        ["P5 max drawdown", fmtPct(mc.p5_max_dd)],
        ["Median max drawdown", fmtPct(mc.p50_max_dd)],
        ["P95 max drawdown", fmtPct(mc.p95_max_dd)],
        ["P95 max consec. losses", mc.p95_max_consec_losses],
        ["Prob. finish positive", fmtPct(mc.prob_finish_positive, 1)],
      ].map(([k, v]) => `<div class="kv-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")
    : "";

  $("#bt-params").textContent = bt.params
    ? Object.entries(bt.params).map(([k, v]) => `  ${k.padEnd(22)} ${v}`).join("\n")
        .replace(/^/, "STRATEGY PARAMETERS\n")
    : "—";
}

function renderScanner(rows) {
  const tbody = $("#table-scanner tbody");
  if (!rows || !rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">Scanner unavailable — data provider not configured</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const regime = r.regime === 1 ? "RISK-ON" : r.regime === -1 ? "RISK-OFF" : "—";
    const regimeCls = r.regime === 1 ? "side-long" : r.regime === -1 ? "side-short" : "";
    const tick = (v, cls) => `<span class="${v ? "pos" : "neg"}">${v ? "✓" : "—"}</span>`;
    return `<tr>
      <td><b>${esc(r.symbol)}</b></td>
      <td class="num">${fmtMoney2(r.close)}</td>
      <td><span class="side-tag ${regimeCls}">${regime}</span></td>
      <td class="num">${r.breakout_long ? "↑ HIGH" : r.breakout_short ? "↓ LOW" : "—"}</td>
      <td class="num">${tick(r.trend_strength)}</td>
      <td class="num">${tick(r.volume_ok)}</td>
      <td>${r.entry_long ? '<span class="side-tag side-long">ENTER LONG</span>'
                          : '<span class="reason">wait</span>'}</td>
      <td class="num">${r.atr != null ? fmtNum(r.atr, 2) : "—"}</td>
    </tr>`;
  }).join("");
}

function renderLogs(logs) {
  $("#log-stream").innerHTML = logs.slice(-200).reverse().map((l) => `
    <div class="log-row">
      <span class="log-ts">${esc(shortTime(l.ts))}</span>
      <span class="log-lvl ${esc(l.level || "INFO")}">${esc(l.level || "INFO")}</span>
      <span class="log-ev">${esc(l.event)}</span>
      <span class="log-extra">${esc(Object.entries(l)
        .filter(([k]) => !["ts", "level", "event"].includes(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join("  "))}</span>
    </div>`).join("");
}

function renderStatus(status, snap) {
  const mode = status.mode || "IDLE";
  $("#v-mode").textContent = mode;
  $("#v-equity").textContent = fmtMoney(snap.equity);
  $("#v-positions").textContent = (snap.positions || []).length;

  const dayPnl = snap.day_pnl;
  const dayEl = $("#v-day");
  dayEl.textContent = dayPnl == null ? "—" : fmtMoney2(dayPnl) +
    (snap.day_return_pct != null ? ` (${fmtPct(snap.day_return_pct)})` : "");
  dayEl.className = "value num " + moneyClass(dayPnl || 0);

  const up = status.uptime_seconds || 0;
  $("#v-uptime").textContent =
    up > 3600 ? `${Math.floor(up / 3600)}h ${Math.floor((up % 3600) / 60)}m`
              : `${Math.floor(up / 60)}m ${Math.floor(up % 60)}s`;

  const pill = $("#pill-mode");
  pill.textContent = status.killed ? "KILLED"
      : status.engine_running ? "RUNNING" : "STOPPED";
  pill.className = "pill " +
    (status.killed || !status.engine_running ? "stopped"
       : mode.toLowerCase().includes("live") ? "live" : "paper");

  // the start/stop pair mirrors the engine's actual state
  if (status.engine_running) {
    $("#btn-start-engine")?.classList.add("hidden");
    $("#btn-stop-engine")?.classList.remove("hidden");
  } else {
    $("#btn-stop-engine")?.classList.add("hidden");
    $("#btn-start-engine")?.classList.remove("hidden");
  }

  $("#sf-kill").textContent = status.killed ? "ENGAGED" : "clear";
  $("#sf-kill").className = status.killed ? "neg" : "pos";
  $("#sf-server").textContent = "v" + (status.version || "?");

  const banner = $("#banner");
  if (status.killed) {
    banner.className = "banner";
    banner.innerHTML = "⛔ <b>KILL SWITCH ENGAGED</b> — all trading halted. Press <b>Resume</b> in the sidebar UI to clear.";
    banner.classList.remove("hidden");
  } else if (!status.engine_running) {
    banner.className = "banner warn";
    banner.textContent = "Engine is stopped — the dashboard is showing last known state.";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

async function refreshAgents() {
  const data = await api("/api/agents");
  if (data) harvestTickerPrices(data);
  renderTicker();
  if (!data) return;
  const agents = data.agents || {};
  $("#agents-src").textContent = data.source === "file"
    ? "from state/agents.json"
    : data.note || "live";

  // ---- agent cards
  const cards = Object.entries(agents).map(([name, s]) => {
    const ok = !s.last_error;
    const state = s.running ? "RUNNING" : "IDLE";
    return `<div class="agent-card ${s.last_error ? "error" : s.running ? "running" : ""}">
        <div class="a-name">${esc(name)}</div>
        <div class="a-role">${esc(s.role || "")}</div>
        <div class="a-meta"><span>state</span>
          <span class="a-state ${ok ? "ok" : "err"}">${state}</span></div>
        <div class="a-meta"><span>cycles</span><span>${esc(s.cycles ?? 0)}</span></div>
        <div class="a-meta"><span>last</span><span>${esc((s.last_duration_ms ?? 0).toFixed(1))}ms</span></div>
        ${s.last_error ? `<div class="a-meta neg" style="margin-top:6px">${esc(s.last_error)}</div>` : ""}
      </div>`;
  }).join("");
  $("#agent-grid").innerHTML = cards ||
    '<div class="empty" style="padding:24px">No agent state — run "py run.py agents"</div>';

  // ---- posteriors
  const probs = data.probabilities || {};
  const probRows = Object.entries(probs).map(([sym, p]) => {
    if (p.note) return `<tr><td><b>${esc(sym)}</b></td><td colspan="5" class="reason">${esc(p.note)}</td></tr>`;
    const ci = p.credible_90 || [];
    const truthy = (p.p_up ?? 0.5) > 0.5;
    return `<tr>
        <td><b>${esc(sym)}</b></td>
        <td class="num ${truthy ? "pos" : "neg"}">${fmtPct(p.p_up, 1)}</td>
        <td class="num">${ci.length ? fmtPct(ci[0], 0) + " – " + fmtPct(ci[1], 0) : "—"}</td>
        <td class="num">${fmtPct(p.confidence, 1)}</td>
        <td class="num">${esc(p.concentration?.toFixed?.(1) ?? p.concentration ?? "—")}</td>
        <td>${truthy ? '<span class="side-tag side-long">UP</span>' : '<span class="side-tag side-short">DOWN</span>'}</td>
      </tr>`;
  }).join("");
  $("#table-posteriors tbody").innerHTML = probRows ||
    `<tr><td colspan="6" class="empty">—</td></tr>`;

  // ---- opportunities
  const plans = data.plans || [];
  $("#table-plans tbody").innerHTML = plans.length ? plans.map((p) => `<tr>
      <td><b>${esc(p.symbol)}</b></td>
      <td>${esc(p.asset_class)}</td>
      <td><span class="side-tag side-${esc(p.side)}">${esc(p.side).toUpperCase()}</span></td>
      <td class="num">${fmtNum(p.payoff, 2)}</td>
      <td class="num">${fmtPct(p.p_win, 1)}</td>
      <td class="num ${signClass(p.expectancy_r)}">${fmtNum(p.expectancy_r, 3)}R</td>
      <td><span class="grade ${esc(p.edge_grade)}">${esc(p.edge_grade)}</span></td>
      <td class="num">${esc((p.quantity ?? 0).toPrecision?.(4) ?? p.quantity ?? "—")}</td>
      <td class="num">${fmtMoney(p.kelly_risk)}</td>
      <td class="reason">${p.guardian_approved ? "approved" : esc((p.guardian_reason || "vetoed").slice(0, 40))}</td>
    </tr>`).join("") : `<tr><td colspan="10" class="empty">No opportunities this cycle</td></tr>`;

  // ---- risk + execution
  const risk = data.risk || {};
  $("#agent-risk").innerHTML = [
    ["Equity", risk.equity != null ? fmtMoney(risk.equity) : "—"],
    ["Drawdown", fmtPct(risk.drawdown)],
    ["Day P&L", moneyClass(risk.day_pl) ? fmtPct(risk.day_pl) : "—"],
    ["Open risk", fmtPct(risk.open_risk_pct)],
    ["Positions", risk.n_positions ?? 0],
    ["Halted", risk.halted ? "YES — " + (risk.halt_reason || "") : "no"],
  ].map(([k, v]) => `<div class="kv-row"><span>${esc(k)}</span><b class="${moneyClass(risk.day_pl)}">${esc(v)}</b></div>`).join("");

  const exec = (data.execution && data.execution.summary) || {};
  $("#agent-exec").innerHTML = [
    ["Open", exec.open ?? 0],
    ["Closed", exec.closed ?? 0],
    ["Win rate", fmtPct(exec.win_rate)],
    ["Realised P&L", fmtMoney(exec.realized_pnl)],
    ["Mode", exec.dry_run ? "DRY RUN" : "LIVE ORDERS"],
  ].map(([k, v]) => `<div class="kv-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");

  // ---- regimes
  const regimes = data.regimes || {};
  $("#table-regimes tbody").innerHTML = Object.keys(regimes).length
    ? Object.entries(regimes).map(([sym, r]) => `<tr>
        <td><b>${esc(sym)}</b></td>
        <td class="num">${fmtMoney2(r.price)}</td>
        <td>${r.regime === 1 ? '<span class="side-tag side-long">UP</span>'
            : r.regime === -1 ? '<span class="side-tag side-short">DOWN</span>'
            : '<span class="reason">FLAT</span>'}</td>
        <td class="num">${fmtNum(r.regime_strength * 40, 1)}</td>
        <td class="num">${fmtNum(r.trend_quality, 2)}</td>
        <td class="num ${signClass(r.momentum)}">${fmtNum(r.momentum, 2)}</td>
        <td class="num">${fmtNum(r.efficiency, 2)}</td>
        <td>${r.breakout_up ? "↑ high" : r.breakout_down ? "↓ low" : "—"}</td>
        <td>${r.stale ? '<span class="neg">stale</span>' : '<span class="pos">fresh</span>'}</td>
      </tr>`).join("")
    : `<tr><td colspan="9" class="empty">—</td></tr>`;
}


/* ============================================================
   Visual pass: ticker tape, sparklines, live dot, status bar
   ============================================================ */

const tickerData = new Map();    // symbol -> {price, prev}

function fmtSignedPct(n) {
  if (n == null || !isFinite(n)) return "—";
  return (n >= 0 ? "+" : "") + (n * 100).toFixed(2) + "%";
}

/**
 * Draw a tiny canvas sparkline. Rendered at devicePixelRatio so it stays
 * crisp on high-DPI screens.
 */
function drawSparkline(canvas, values, color) {
  if (!canvas || !values || values.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const w = 62, h = 22;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const lo = Math.min(...values), hi = Math.max(...values);
  const span = (hi - lo) || 1;
  const X = (i) => (i / (values.length - 1)) * (w - 2) + 1;
  const Y = (v) => h - 2 - ((v - lo) / span) * (h - 4);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "44");
  grad.addColorStop(1, color + "00");
  ctx.beginPath();
  ctx.moveTo(X(0), Y(values[0]));
  for (let i = 1; i < values.length; i++) ctx.lineTo(X(i), Y(values[i]));
  ctx.lineTo(X(values.length - 1), h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(X(0), Y(values[0]));
  for (let i = 1; i < values.length; i++) ctx.lineTo(X(i), Y(values[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  ctx.lineJoin = "round";
  ctx.stroke();
}

/** Render the scrolling ticker from whatever prices we have most recently. */
function renderTicker() {
  const track = $("#ticker-track");
  if (!track) return;
  const entries = [...tickerData.entries()];
  if (!entries.length) {
    track.innerHTML = '<span class="tick" style="color:var(--text-faint)">start the engine to see live prices</span>';
    return;
  }
  const chips = entries.map(([sym, d]) => {
    const up = (d.prev != null && d.price >= d.prev) || d.prev == null;
    const cls = up ? "up" : "down";
    const ch = d.prev != null && d.prev !== 0
      ? fmtSignedPct((d.price - d.prev) / d.prev) : "—";
    return `<span class="tick"><span class="t-sym">${esc(sym)}</span>`
        + `<span class="t-px">${d.price > 100 ? d.price.toFixed(2) : d.price.toFixed(4)}</span>`
        + `<span class="t-ch ${cls}">${ch}</span></span>`;
  }).join("");
  // duplicated so the CSS translateX(-50%) loops seamlessly
  track.innerHTML = chips + chips;
}

/** Pull the freshest price per symbol out of whatever view last saw it. */
function harvestTickerPrices(agents) {
  const source = (agents && agents.probabilities) || {};
  const regimes = (agents && agents.regimes) || {};
  for (const [sym, reg] of Object.entries(regimes)) {
    const price = reg && reg.price;
    if (typeof price !== "number" || price <= 0) continue;
    const existing = tickerData.get(sym);
    tickerData.set(sym, {
      price,
      prev: existing ? existing.price : price,
    });
  }
}

function updateLiveDot(alive, killed) {
  const dot = $("#live-dot");
  if (!dot) return;
  dot.className = "live-dot " + (killed ? "killed" : alive ? "alive" : "dead");
  dot.title = killed ? "kill switch engaged"
      : alive ? "engine running" : "engine stopped";
}

function updateStatusBar(status, snap) {
  if (!status) return;
  const set = (id, text, cls) => {
    const el = document.getElementById(id);
    if (el) { el.textContent = text; if (cls) el.className = cls; }
  };
  set("sb-engine", status.engine_running ? "running" : "stopped",
      status.engine_running ? "pos" : "neg");
  set("sb-broker", snap && snap.broker ? snap.broker : "paper");
  const riskPct = (snap && snap.risk && typeof snap.risk.open_risk === "number")
    ? snap.risk.open_risk : null;
  set("sb-risk", riskPct != null ? fmtMoney(riskPct) : "—");
  set("sb-pos", (snap && snap.positions ? snap.positions.length : 0));
  const day = snap ? snap.day_pnl : null;
  set("sb-day", day == null ? "—" : fmtMoney2(day), moneyClass(day || 0));
  set("sb-version", "v" + (status.version || "?"));
  set("sb-latency", status.server_time
    ? Math.max(0, Math.round((Date.now() - new Date(status.server_time).getTime()) / 1000)) + "s"
    : "—");
  updateLiveDot(status.engine_running, status.killed);
}


/* ============================================================
   FORWARD TRACKING — did the edge survive reality?
   ============================================================ */

const forwardChart = null;   // created lazily so the canvas exists

function verdictClass(text) {
  const t = String(text || "").toLowerCase();
  if (t.startsWith("drifting")) return "drifting";
  if (t.startsWith("partial")) return "partial";
  if (t.startsWith("too early")) return "early";
  if (t.startsWith("tracking")) return "tracking";
  return "none";
}

function verdictTag(cls) {
  return { tracking: "tracking", early: "too early", partial: "partial drift",
           drifting: "drifting", none: "no data" }[cls] || "no data";
}

function renderForward(data) {
  if (!data) return;
  const badge = $("#badge-forward");
  if (badge) badge.textContent = data.n_trades || 0;

  const verdict = $("#forward-verdict");
  const cls = verdictClass(data.verdict);
  verdict.className = "verdict " + cls;
  verdict.innerHTML = `<span class="verdict-class">${esc(verdictTag(cls))}</span>`
    + esc(data.verdict || "");

  // ---- KPI tiles
  const kpis = [
    kpiTile("Trades", data.n_trades || 0, `${data.n_wins ?? 0}W / ${data.n_losses ?? 0}L`,
            "neu", "#38bdf8"),
    kpiTile("Win rate", fmtPct(data.win_rate),
            data.win_rate_ci ? `CI ${fmtPct(data.win_rate_ci[0], 0)}–${fmtPct(data.win_rate_ci[1], 0)}` : "",
            "neu", "#a78bfa"),
    kpiTile("Expectancy", (data.expectancy_r != null ? fmtNum(data.expectancy_r, 3) + "R" : "—"),
            data.expectancy_r_ci ? `CI ${fmtNum(data.expectancy_r_ci[0], 2)}–${fmtNum(data.expectancy_r_ci[1], 2)}` : "",
            (data.expectancy_r || 0) > 0 ? "pos" : "neg",
            (data.expectancy_r || 0) > 0 ? "#34d399" : "#f87171"),
    kpiTile("Total P&L", fmtMoney2(data.total_pnl), `${data.n_trades || 0} forward trades`,
            moneyClass(data.total_pnl || 0), "#38bdf8"),
    kpiTile("Profit factor", fmtNum(data.profit_factor),
            data.avg_win_r != null ? `avg win ${fmtNum(data.avg_win_r, 2)}R` : "",
            (data.profit_factor || 0) > 1 ? "pos" : "neg", "#fbbf24"),
    kpiTile("Max drawdown", fmtMoney2(data.max_drawdown), "peak-to-trough of forward P&L",
            "neg", "#f87171"),
  ];
  $("#forward-kpis").innerHTML = kpis.join("");

  // ---- equity curve
  const curve = data.equity_curve || [];
  const canvas = $("#chart-forward");
  if (curve.length > 1 && canvas) {
    const values = curve.map((c) => c.pnl);
    charts.forward = charts.forward || new Chart(canvas);
    charts.forward.set(
      [{ color: "#34d399", data: values, fill: true, width: 1.9 },
       { color: "#526475", data: values.map(() => 0), width: 1, dash: [4, 4] }],
      { money: true, includeZero: true,
        labels: curve.map((c) => shortTime(c.t).slice(0, 10)) });
  } else if (charts.forward) {
    charts.forward.set([], {});
  }
  const meta = $("#fwd-curve-meta");
  if (meta) meta.textContent = curve.length ? `${curve.length} trades` : "no trades yet";

  // ---- drift table
  const drift = data.drift || {};
  const metrics = drift.metrics || {};
  const rows = Object.entries(metrics).map(([name, m]) => {
    const predicted = name === "win_rate" ? fmtPct(m.predicted)
        : fmtNum(m.predicted, 3) + "R";
    const realised = name === "win_rate" ? fmtPct(data.win_rate)
        : fmtNum(data.expectancy_r, 3) + "R";
    const ci = `[${name === "win_rate" ? fmtPct(m.ci_low, 0) : fmtNum(m.ci_low, 2)} – `
        + `${name === "win_rate" ? fmtPct(m.ci_high, 0) : fmtNum(m.ci_high, 2)}]`;
    const badgeHtml = m.outside_ci
      ? '<span class="side-tag side-short">outside</span>'
      : '<span class="side-tag side-long">within</span>';
    return `<tr><td><b>${esc(name)}</b></td>
      <td class="num">${predicted}</td><td class="num">${realised}</td>
      <td class="num">${ci}</td><td class="num ${signClass(-m.gap)}">${fmtNum(m.gap, 3)}</td>
      <td>${badgeHtml}</td></tr>`;
  }).join("");
  const body = $("#table-drift tbody");
  body.innerHTML = rows ||
    `<tr><td colspan="6" class="empty">${
      drift.status === "no baseline"
        ? "capture a baseline from the last backtest"
        : "not enough trades to compare yet"}</td></tr>`;

  // ---- breakdowns
  const fill = (tableId, source) => {
    const rowsHtml = Object.entries(source || {}).map(([k, v]) => `<tr>
        <td><b>${esc(k)}</b></td><td class="num">${v.trades}</td>
        <td class="num">${fmtPct(v.win_rate)}</td>
        <td class="num ${signClass(v.total_r)}">${fmtNum(v.total_r, 1)}R</td>
        <td class="num ${moneyClass(v.pnl)}">${fmtMoney2(v.pnl)}</td></tr>`).join("");
    $(tableId + " tbody").innerHTML = rowsHtml ||
      `<tr><td colspan="5" class="empty">—</td></tr>`;
  };
  fill("#table-fwd-strategy", data.by_strategy);
  fill("#table-fwd-symbol", data.by_symbol);
}

async function loadForward() {
  const data = await api("/api/forward");
  if (data) renderForward(data);
}

function bindForwardUI() {
  $("#btn-fwd-baseline")?.addEventListener("click", async () => {
    const res = await api("/api/forward/baseline", {
      method: "POST", body: JSON.stringify({ from_backtest: true }),
    });
    toast(res && res.ok ? "baseline set from the last backtest"
        : (res && res.error ? res.error : "no backtest found"),
        res && res.ok ? "ok" : "err");
    if (res && res.ok) loadForward();
  });
  $("#btn-fwd-import")?.addEventListener("click", async () => {
    const res = await api("/api/forward/import", { method: "POST", body: "{}" });
    toast(res ? `imported ${res.imported} trade(s)` : "import failed",
          res && res.ok ? "ok" : "err");
    if (res && res.ok) loadForward();
  });
  $("#btn-fwd-refresh")?.addEventListener("click", loadForward);
  $("#btn-fwd-reset")?.addEventListener("click", async () => {
    if (!confirm("Clear every recorded forward trade? This cannot be undone.")) return;
    const res = await api("/api/forward/reset", { method: "POST", body: "{}" });
    toast(res ? "journal cleared" : "failed", res ? "ok" : "err");
    if (res) loadForward();
  });
}


/* ============================================================
   DATA SOURCE — Binance public for crypto, Yahoo as fallback
   ============================================================ */

let dataSources = [];

async function loadDataSources() {
  const data = await api("/api/data-sources");
  if (!data || !data.sources) return;
  dataSources = data.sources;
  const sel = $("#f-data-source");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = data.sources.map((s) =>
    `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
  if (current) sel.value = current;
  updateDataSourceNote();
}

function updateDataSourceNote() {
  const sel = $("#f-data-source");
  const note = $("#data-source-note");
  if (!sel || !note) return;
  const source = dataSources.find((s) => s.id === sel.value);
  if (!source) return;
  const limits = {
    router: "Crypto: Binance public, no key. Everything else: Yahoo. "
            + "Symbols no source can serve are dropped, not fatal.",
    binance: "Crypto only, no API key. Fastest option.",
    alpaca: "Crypto is public. Equities need a free Alpaca key in Setup > Broker.",
    yahoo: "Everything, but rate-limited at 1m across many symbols.",
  };
  note.innerHTML = `<b>${esc(source.label)}</b> — ${esc(limits[sel.value] || source.note)}`;
}

function bindDataSourceUI() {
  $("#f-data-source")?.addEventListener("change", updateDataSourceNote);
  $("#btn-fill-defaults")?.addEventListener("click", () => {
    setTimeout(updateDataSourceNote, 50);
  });
}


/* ============================================================
   LIVE STREAM - server-sent events so the UI pushes rather than polls
   ============================================================ */

function connectStream() {
  if (typeof EventSource === "undefined") return;   // old browser: poller covers it
  try {
    const source = new EventSource("/api/stream");
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "snapshot" && payload.data) {
          applyLiveUpdate(payload.data);
        }
      } catch (err) {
        /* malformed frame: the poller resyncs */
      }
    };
  } catch (err) {
    /* SSE unavailable: the 1s poller is the fallback */
  }
}

function applyLiveUpdate(snap) {
  if (!snap) return;
  updateTickerFromSnapshot(snap);
  updateStatusBarFromSnapshot(snap);
  const active = document.querySelector(".nav-item.active");
  const view = active ? active.dataset.view : "overview";
  if (view === "overview" && typeof renderOverview === "function") renderOverview(snap, lastMetrics);
  if (view === "positions" && typeof renderPositions === "function") renderPositions(snap.positions || []);
}

function updateTickerFromSnapshot(snap) {
  const track = document.getElementById("ticker-track");
  if (!track) return;
  const rows = (snap.quotes && snap.quotes.length) ? snap.quotes : [];
  if (!rows.length) return;
  const html = rows.map((q) => {
    const up = (q.change_pct || 0) >= 0;
    return '<span class="tick"><span class="t-sym">' + esc(q.symbol) + '</span>' +
      '<span class="t-px">' + esc(q.price) + '</span>' +
      '<span class="t-ch ' + (up ? 'up' : 'down') + '">' + (up ? '+' : '') +
      (q.change_pct || 0).toFixed(2) + '%</span></span>';
  }).join("");
  track.innerHTML = html + html;
}

function updateStatusBarFromSnapshot(snap) {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set("sb-day", fmtMoney2(snap.day_pnl));
  set("sb-pos", (snap.positions || []).length);
}

/* ---------------------------------------------------------- polling */
async function refresh(scannerToo = false) {
  const [status, snap, metrics, trades, logs] = await Promise.all([
    api("/api/status"),
    api("/api/snapshot"),
    api("/api/metrics"),
    api("/api/trades?limit=400"),
    api("/api/logs?limit=120"),
  ]);

  if (status && snap) {
    renderStatus(status, snap);
    updateStatusBar(status, snap);
    renderOverview(snap, metrics);
    renderPositions((snap.positions) || []);
  }
  if (trades) renderTrades(trades.trades || []);
  if (metrics) renderBacktest(metrics.backtest && Object.keys(metrics.backtest).length
    ? cachedBacktest : cachedBacktest);
  if (logs) renderLogs(logs.logs || []);
  if (scannerToo) {
    const sc = await api("/api/control/scanner");
    if (sc) renderScanner(sc.rows);
  }
}

async function refreshBacktest() {
  const bt = await api("/api/backtest");
  if (bt && Object.keys(bt).length) renderBacktest(bt);
}

async function refreshScanner() {
  const sc = await api("/api/control/scanner");
  if (sc) renderScanner(sc.rows);
}


/* ---------------------------------------------------------- engine control */

async function startEngine() {
  const btn = $("#btn-start-engine");
  const mode = $("#engine-mode").value;
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> STARTING';
  const res = await api("/api/engine/start", {
    method: "POST",
    body: JSON.stringify({ mode, dry_run: true, poll_seconds: 60 }),
  });
  btn.disabled = false;
  btn.textContent = originalLabel;
  if (res && res.started) {
    toast(`Engine started (${mode})`, "ok");
    $("#btn-start-engine").classList.add("hidden");
    $("#btn-stop-engine").classList.remove("hidden");
    refresh(true);
  } else {
    toast(res && res.error ? res.error
        : (res && res.reason ? res.reason : "could not start the engine"), "err");
  }
}

async function stopEngine() {
  const res = await api("/api/engine/stop", { method: "POST", body: "{}" });
  if (res) {
    toast("Engine stopped", "ok");
    $("#btn-stop-engine").classList.add("hidden");
    $("#btn-start-engine").classList.remove("hidden");
    refresh(true);
  } else {
    toast("could not stop the engine", "err");
  }
}

$("#btn-start-engine").addEventListener("click", startEngine);
$("#btn-stop-engine").addEventListener("click", stopEngine);


/* ============================================================
   CONTROL PANEL — every command as a card with switches/selects
   ============================================================ */

const cmdState = { cards: [], selected: null, values: {}, outputLines: [] };

function renderCmdCards() {
  const grid = $("#cmd-grid");
  if (!grid) return;
  grid.innerHTML = cmdState.cards.map((c) => `
    <button class="cmd-card ${cmdState.selected === c.command ? "active" : ""}"
            data-cmd="${esc(c.command)}" title="${esc(c.description)}">
      <div class="c-title">${esc(c.label)}</div>
      <div class="c-desc">${esc(c.description)}</div>
    </button>`).join("");
  grid.querySelectorAll(".cmd-card").forEach((card) => {
    card.addEventListener("click", () => selectCommand(card.dataset.cmd));
  });
}

function argControl(name, spec, value) {
  const label = (spec.help || name).replace(/_/g, " ");
  const id = `arg-${name}`;
  if (spec.type === "flag") {
    return `<label class="field"><span class="field-label">${esc(label)}</span>
      <div class="switch-row">
        <label class="switch"><input type="checkbox" id="${id}" data-arg="${esc(name)}"
          ${value ? "checked" : ""}><span class="slider"></span></label>
        <span class="switch-label">${value ? "on" : "off"}</span>
      </div></label>`;
  }
  if (spec.type === "choice") {
    const options = (spec.options || [])
      .map((o) => `<option value="${esc(o)}" ${o === value ? "selected" : ""}>${esc(o)}</option>`)
      .join("");
    return `<label class="field"><span class="field-label">${esc(label)}</span>
      <select class="select" id="${id}" data-arg="${esc(name)}">${options}</select></label>`;
  }
  if (spec.type === "number") {
    return `<label class="field"><span class="field-label">${esc(label)}</span>
      <input class="input num" type="number" id="${id}" data-arg="${esc(name)}"
             value="${esc(value)}" step="any"></label>`;
  }
  return `<label class="field"><span class="field-label">${esc(label)}</span>
    <input class="input" id="${id}" data-arg="${esc(name)}" value="${esc(value)}"></label>`;
}

function selectCommand(command) {
  const spec = cmdState.cards.find((c) => c.command === command);
  if (!spec) return;
  cmdState.selected = command;
  cmdState.values = {};
  (spec.args || []).forEach((a) => {
    cmdState.values[a.name] = a.default;
  });
  $("#cmd-detail-name").textContent = spec.label || command;
  $("#cmd-detail-desc").textContent = spec.description || "";

  const warn = $("#cmd-warning");
  if (spec.warning) {
    warn.textContent = "! " + spec.warning;
    warn.classList.remove("hidden");
  } else {
    warn.classList.add("hidden");
  }

  // subcommands (cache list/clear) become a selector, never a typed word
  const args = [...(spec.args || [])];
  if (spec.requires_subcommand) {
    args.unshift({
      name: "cache_command", type: "choice",
      options: ["list", "clear"], default: "list",
      help: "what to do with the cache",
    });
  }
  $("#cmd-form").innerHTML = args.length
    ? args.map((a) => argControl(a.name, a, cmdState.values[a.name])).join("")
    : '<div class="empty" style="padding:14px;color:var(--text-faint)">No options.</div>';

  $("#cmd-form").querySelectorAll("[data-arg]").forEach((el) => {
    el.addEventListener("change", () => {
      const name = el.dataset.arg;
      if (el.type === "checkbox") {
        cmdState.values[name] = el.checked;
        el.closest(".switch-row").querySelector(".switch-label").textContent =
          el.checked ? "on" : "off";
      } else if (el.type === "number") {
        cmdState.values[name] = el.value === "" ? 0 : Number(el.value);
      } else {
        cmdState.values[name] = el.value;
      }
    });
  });
  renderCmdCards();
}

function resetCommandDefaults() {
  if (!cmdState.selected) return;
  const spec = cmdState.cards.find((c) => c.command === cmdState.selected);
  if (spec) selectCommand(spec.command);
}

async function runSelectedCommand() {
  if (!cmdState.selected) {
    toast("Pick a command first", "err");
    return;
  }
  const btn = $("#btn-cmd-run");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> RUNNING';
  const res = await api("/api/command/run", {
    method: "POST",
    body: JSON.stringify({ command: cmdState.selected, args: cmdState.values }),
  });
  if (res && res.ok) {
    toast(`${res.run.label} started`, "ok");
    cmdState.outputLines = [];
    pollCommandOutput(true);
  } else {
    toast(res && res.error ? res.error : "could not start the command", "err");
    btn.disabled = false;
    btn.textContent = "▶ RUN";
  }
}

async function pollCommandOutput(force) {
  const status = await api("/api/command/status");
  if (!status) return;
  const pill = $("#cmd-pill");
  if (pill) {
    pill.textContent = status.running ? "running" : "idle";
    pill.className = "pill " + (status.running ? "paper" : "");
  }
  const btn = $("#btn-cmd-run");
  if (btn && !status.running) {
    btn.disabled = false;
    btn.textContent = "▶ RUN";
  }

  const out = await api("/api/command/output?lines=260");
  if (out && out.lines) {
    const text = out.lines.join("\n");
    const pre = $("#cmd-output");
    if (pre) pre.textContent = text || "(no output yet)";
    const meta = $("#cmd-out-meta");
    if (meta) {
      const cur = status.current;
      meta.textContent = cur
        ? `${cur.command} — ${cur.running ? "running" : "exit " + cur.exit_code} — ${out.lines.length} lines`
        : "no command run yet";
    }
  }

  if (status.current) {
    const tbody = $("#table-runs tbody");
    const rows = status.history.map((h) => `<tr>
        <td><b>${esc(h.label)}</b></td>
        <td class="num">${esc(shortTime(h.started_at))}</td>
        <td class="num">${h.finished_at ? esc(shortTime(h.finished_at)) : "—"}</td>
        <td class="num ${h.exit_code === 0 ? "pos" : h.exit_code ? "neg" : "neu"}">${h.exit_code ?? "—"}</td>
        <td>${h.running ? '<span class="reason stop">running</span>'
                        : '<span class="pos">done</span>'}</td>
      </tr>`).join("");
    if (tbody) {
      tbody.innerHTML = rows || '<tr><td colspan="5" class="empty">nothing run yet</td></tr>';
    }
  }
}

async function stopCommand() {
  const res = await api("/api/command/stop", { method: "POST", body: "{}" });
  toast(res && res.stopped ? "command stopped" : "nothing is running",
        res && res.stopped ? "ok" : "err");
}

/* ============================================================
   SETUP PANEL — broker, strategy, instruments, risk
   ============================================================ */

function setSwitch(el, on) {
  if (!el) return;
  el.checked = !!on;
  const label = el.closest(".switch-row")?.querySelector(".switch-label");
  if (label && label.textContent.trim() === "on") label.textContent = on ? "on" : "off";
  if (label && label.textContent.trim() === "off") label.textContent = on ? "on" : "off";
}

function syncSwitches() {
  // re-label the generic on/off pairs now that their state is known
  document.querySelectorAll(".switch-row").forEach((row) => {
    const toggle = row.querySelector("input[type=checkbox]");
    const label = row.querySelector(".switch-label");
    if (!toggle || !label) return;
    if (label.dataset.dynamic) label.textContent = toggle.checked ? "on" : "off";
  });
}

async function loadBrokers() {
  const data = await api("/api/brokers");
  if (!data) return;
  const sel = $("#f-broker");
  const current = sel.value;
  sel.innerHTML = data.brokers.map((b) =>
    `<option value="${esc(b.id)}">${esc(b.label)}</option>`).join("");
  if (current) sel.value = current;
  updateBrokerNote();
}

function updateBrokerNote() {
  const sel = $("#f-broker");
  const note = $("#broker-note");
  const testnet = $("#f-broker-testnet");
  const id = sel.value;
  const labels = {
    paper: "No account needed. Fills are simulated against live prices.",
    binance: testnet.checked
      ? "Testnet: generate free keys at testnet.binance.vision and paste them here."
      : "LIVE funds. The server also needs BINANCE_LIVE_CONFIRMED=yes set in its environment.",
    alpaca: testnet.checked
      ? "Paper keys from alpaca.markets. Set ALPACA_KEY_ID / ALPACA_SECRET_KEY."
      : "LIVE funds. The server also needs ALPACA_LIVE_CONFIRMED=yes.",
    exness: "Exness has no REST API — access is through the MT5 terminal: Windows, "
            + "MetaTrader 5 installed and running, your account logged in, "
            + "algorithmic trading enabled, and 'py -m pip install MetaTrader5'.",
    generic: "Orders are written as JSON to a watched directory or POSTed to a webhook. "
             + "You wire the receiving side.",
  };
  note.textContent = labels[id] || "";
  $("#lbl-broker-testnet").textContent = testnet.checked
    ? "Testnet / paper" : "LIVE endpoint";
}

async function saveBroker() {
  const body = {
    section: "broker",
    values: {
      broker: $("#f-broker").value,
      account_label: $("#f-broker-label").value,
      testnet: $("#f-broker-testnet").checked,
      store_credentials: $("#f-broker-store").checked,
    },
  };
  if ($("#f-broker-key").value) body.values.key = $("#f-broker-key").value;
  if ($("#f-broker-secret").value) body.values.secret = $("#f-broker-secret").value;
  if ($("#f-broker-passphrase").value) body.values.passphrase = $("#f-broker-passphrase").value;

  const res = await api("/api/settings", {
    method: "POST", body: JSON.stringify(body),
  });
  if (res && res.ok) {
    toast("Connection saved", "ok");
    $("#f-broker-secret").value = "";
  } else {
    toast(res && res.error ? res.error : "could not save", "err");
  }
  updateBrokerNote();
}

async function testBroker() {
  const box = $("#broker-test");
  box.classList.remove("hidden", "ok", "bad");
  box.innerHTML = "testing ...";
  const res = await api("/api/broker/test", {
    method: "POST",
    body: JSON.stringify({ broker: $("#f-broker").value }),
  });
  if (!res) {
    box.className = "broker-test bad";
    box.innerHTML = "no response from the server";
    return;
  }
  box.className = "broker-test " + (res.ok ? "ok" : "bad");
  let html = `<div class="bt-line"><b>${res.ok ? "connected" : "failed"}</b> — ${esc(res.broker || "")}</div>`;
  if (res.account) {
    html += `<div class="bt-line">equity ${fmtMoney(res.account.equity)} · `
         + `cash ${fmtMoney(res.account.cash)} · ${esc(res.account.currency || "")}</div>`;
  }
  if (res.error) html += `<div class="bt-line neg">${esc(res.error)}</div>`;
  if (res.hint) html += `<div class="bt-line warn">${esc(res.hint)}</div>`;
  box.innerHTML = html;
}

async function saveSection(section, values, button) {
  const res = await api("/api/settings", {
    method: "POST", body: JSON.stringify({ section, values }),
  });
  const label = button && button.dataset.label ? button.dataset.label : section;
  if (res && res.ok) {
    toast(`${label} saved`, "ok");
    return true;
  }
  toast(res && res.error ? res.error : "could not save", "err");
  return false;
}

function collectStrategy() {
  return {
    run_trend_breakout: $("#f-run-trend").checked,
    run_multi_agent: $("#f-run-agents").checked,
    entry_lookback: Number($("#f-entry-lookback").value || 20),
    exit_lookback: Number($("#f-exit-lookback").value || 10),
    regime_ma_period: Number($("#f-regime-ma").value || 200),
    adx_min: Number($("#f-adx-min").value || 20),
    atr_stop_mult: Number($("#f-atr-stop").value || 2),
    atr_trail_mult: Number($("#f-atr-trail").value || 3),
    kelly_fraction: Number($("#f-kelly").value || 0.25),
    min_expectancy: Number($("#f-min-exp").value || 0.2),
    min_confidence: Number($("#f-min-conf").value || 0.65),
    allow_shorts: $("#f-shorts").checked,
    require_volume: $("#f-volume").checked,
  };
}

function collectInstruments() {
  const classes = [];
  if ($("#f-class-index").checked) classes.push("index");
  if ($("#f-class-forex").checked) classes.push("forex");
  if ($("#f-class-crypto").checked) classes.push("crypto");
  return {
    data_source: $("#f-data-source") ? $("#f-data-source").value : "router",
    timeframe: $("#f-timeframe").value,
    range_: $("#f-range").value,
    asset_classes: classes.length ? classes : ["crypto"],
    max_symbols: Number($("#f-max-symbols").value || 20),
    use_market_gate: $("#f-use-gate").checked,
    market_gate_symbol: $("#f-gate").value || "SPY",
  };
}

function collectRisk() {
  return {
    risk_per_trade: Number($("#f-risk-trade").value || 0.01),
    max_position_pct: Number($("#f-max-pos-pct").value || 0.2),
    max_positions: Number($("#f-max-pos").value || 8),
    max_total_risk: Number($("#f-max-risk").value || 0.06),
    max_fraction: Number($("#f-max-frac").value || 0.05),
    daily_loss_limit: Number($("#f-daily").value || 0.03),
    max_drawdown_halt: Number($("#f-dd").value || 0.15),
    bankroll: Number($("#f-start-balance").value || 100000),
    capital: Number($("#f-start-balance").value || 100000),
    slippage_bps: Number($("#f-slippage").value || 5),
    commission_per_share: Number($("#f-commission").value || 0.005),
    dry_run: $("#f-dry-run").checked,
    kill_on_risk: $("#f-kill-risk").checked,
  };
}

async function applyPropFirm() {
  const firmware = $("#f-prop").value;
  const note = $("#prop-note");
  note.textContent = "applying ...";
  const res = await api("/api/propfirm/select", {
    method: "POST",
    body: JSON.stringify({
      firm: firmware,
      starting_balance: Number($("#f-start-balance").value || 100000),
      safety_buffer: Number($("#f-buffer").value || 0.25),
    }),
  });
  if (!res || !res.ok) {
    note.textContent = (res && res.error) ? res.error : "could not apply the profile";
    return;
  }
  const r = res.risk_applied;
  note.innerHTML = `<b>${esc(res.firm.name)}</b> applied. Your limits are now `
    + `risk ${(r.risk_per_trade * 100).toFixed(2)}% per trade, `
    + `daily stop ${(r.daily_loss_limit * 100).toFixed(2)}%, `
    + `drawdown halt ${(r.max_drawdown_halt * 100).toFixed(2)}% — `
    + `all inside the firm's own ${(res.firm.max_daily_loss_pct * 100).toFixed(0)}% daily `
    + `and ${(res.firm.max_drawdown_pct * 100).toFixed(0)}% drawdown rules, `
    + `leaving a buffer for gaps and slippage.`;
  // reflect the applied limits back into the form
  $("#f-risk-trade").value = r.risk_per_trade;
  $("#f-daily").value = r.daily_loss_limit;
  $("#f-dd").value = r.max_drawdown_halt;
  $("#f-max-risk").value = r.max_total_risk;
  $("#f-max-pos").value = r.max_positions;
  toast("Prop-firm profile applied", "ok");
}

async function loadPropFirms() {
  const data = await api("/api/propfirms");
  if (!data) return;
  const sel = $("#f-prop");
  sel.innerHTML = data.firms.map((f) =>
    `<option value="${esc(f.firm)}">${esc(f.name)} — ${(f.max_daily_loss_pct * 100).toFixed(0)}% daily / ${(f.max_drawdown_pct * 100).toFixed(0)}% DD</option>`).join("");
}

function bindControlUI() {
  $("#btn-cmd-run")?.addEventListener("click", runSelectedCommand);
  $("#btn-cmd-stop")?.addEventListener("click", stopCommand);
  $("#btn-cmd-defaults")?.addEventListener("click", resetCommandDefaults);
  $("#btn-cmd-clear")?.addEventListener("click", () => {
    cmdState.outputLines = [];
    const pre = $("#cmd-output");
    if (pre) pre.textContent = "";
  });

  $("#btn-refresh-brokers")?.addEventListener("click", loadBrokers);
  $("#f-broker")?.addEventListener("change", updateBrokerNote);
  $("#f-broker-testnet")?.addEventListener("change", updateBrokerNote);
  $("#btn-broker-save")?.addEventListener("click", saveBroker);
  $("#btn-broker-test")?.addEventListener("click", testBroker);
  $("#btn-broker-disconnect")?.addEventListener("click", async () => {
    const res = await api("/api/broker/disconnect", { method: "POST", body: "{}" });
    toast(res && res.ok ? "Disconnected" : "failed", res && res.ok ? "ok" : "err");
  });

  $("#btn-save-strategy")?.addEventListener("click", (e) =>
    saveSection("strategy", collectStrategy(), e.currentTarget));
  $("#btn-save-instruments")?.addEventListener("click", (e) =>
    saveSection("instruments", collectInstruments(), e.currentTarget));
  $("#btn-fill-defaults")?.addEventListener("click", async () => {
    const data = await api("/api/instruments");
    if (!data || !data.defaults) return;
    $("#f-class-index").checked = !!data.defaults.index;
    $("#f-class-forex").checked = !!data.defaults.forex;
    $("#f-class-crypto").checked = !!data.defaults.crypto;
    syncSwitches();
    toast("Class defaults filled in", "ok");
  });
  $("#btn-apply-prop")?.addEventListener("click", applyPropFirm);
  $("#btn-refresh-prop")?.addEventListener("click", loadPropFirms);
  $("#btn-save-risk")?.addEventListener("click", (e) =>
    saveSection("risk", collectRisk(), e.currentTarget));
}

async function loadSetupForms() {
  const [settings, firms] = await Promise.all([
    api("/api/settings"), api("/api/propfirms"),
  ]);
  if (settings) {
    const b = settings.broker || {};
    if (b.broker && $("#f-broker").value !== b.broker) $("#f-broker").value = b.broker;
    $("#f-broker-label").value = b.account_label || "";
    setSwitch($("#f-broker-testnet"), b.testnet !== false);
    setSwitch($("#f-broker-store"), !!b.stored_in_file);
    updateBrokerNote();

    const s = settings.strategy || {};
    setSwitch($("#f-run-trend"), s.run_trend_breakout !== false);
    setSwitch($("#f-run-agents"), s.run_multi_agent !== false);
    $("#f-entry-lookback").value = s.entry_lookback ?? 20;
    $("#f-exit-lookback").value = s.exit_lookback ?? 10;
    $("#f-regime-ma").value = s.regime_ma_period ?? 200;
    $("#f-adx-min").value = s.adx_min ?? 20;
    $("#f-atr-stop").value = s.atr_stop_mult ?? 2;
    $("#f-atr-trail").value = s.atr_trail_mult ?? 3;
    $("#f-kelly").value = s.kelly_fraction ?? 0.25;
    $("#f-min-exp").value = s.min_expectancy ?? 0.2;
    $("#f-min-conf").value = s.min_confidence ?? 0.65;
    setSwitch($("#f-shorts"), !!s.allow_shorts);
    setSwitch($("#f-volume"), !!s.require_volume);

    const i = settings.instruments || {};
    if (i.data_source && $("#f-data-source")) {
      $("#f-data-source").value = i.data_source;
      updateDataSourceNote();
    }
    if (i.timeframe) $("#f-timeframe").value = i.timeframe;
    if (i.range_) $("#f-range").value = i.range_;
    $("#f-max-symbols").value = i.max_symbols ?? 20;
    $("#f-gate").value = i.market_gate_symbol || "SPY";
    setSwitch($("#f-use-gate"), i.use_market_gate !== false);
    setSwitch($("#f-class-index"), (i.asset_classes || []).includes("index"));
    setSwitch($("#f-class-forex"), (i.asset_classes || []).includes("forex"));
    setSwitch($("#f-class-crypto"), (i.asset_classes || []).includes("crypto"));

    const r = settings.risk || {};
    $("#f-risk-trade").value = r.risk_per_trade ?? 0.01;
    $("#f-max-pos-pct").value = r.max_position_pct ?? 0.2;
    $("#f-max-pos").value = r.max_positions ?? 8;
    $("#f-max-risk").value = r.max_total_risk ?? 0.06;
    $("#f-max-frac").value = r.max_fraction ?? 0.05;
    $("#f-daily").value = r.daily_loss_limit ?? 0.03;
    $("#f-dd").value = r.max_drawdown_halt ?? 0.15;
    $("#f-start-balance").value = r.bankroll ?? 100000;
    $("#f-slippage").value = r.slippage_bps ?? 5;
    $("#f-commission").value = r.commission_per_share ?? 0.005;
    setSwitch($("#f-dry-run"), r.dry_run !== false);
    setSwitch($("#f-kill-risk"), r.kill_on_risk !== false);
  }
  if (firms) loadPropFirms();
  syncSwitches();
}

async function loadCommandCatalogue() {
  const data = await api("/api/command/list");
  if (!data || !data.commands) return;
  cmdState.cards = data.commands;
  renderCmdCards();
}

/* ---------------------------------------------------------- events */
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const view = btn.dataset.view;
    $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
    if (view === "backtest") refreshBacktest();
    if (view === "agents") refreshAgents();
    if (view === "control") { loadCommandCatalogue(); pollCommandOutput(true); }
    if (view === "forward") loadForward();
    if (view === "setup") { loadBrokers(); loadSetupForms(); loadDataSources(); }
    if (view === "scanner") refreshScanner();
    if (view === "trades") renderTrades(cachedTrades);
    if (view === "overview" && charts.hist) charts.hist.resolution?.();
  });
});

$$(".chip[data-filter]").forEach((chip) => {
  chip.addEventListener("click", () => {
    $$(".chip[data-filter]").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    tradeFilter = chip.dataset.filter;
    renderTrades(cachedTrades);
  });
});

$("#btn-kill").addEventListener("click", async () => {
  if (!confirm("KILL SWITCH\n\nFlatten all positions and halt all trading. Continue?")) return;
  const res = await api("/api/control/kill", {
    method: "POST",
    body: JSON.stringify({ reason: "manual kill switch from dashboard" }),
  });
  toast(res && res.killed ? "Kill switch engaged" : "Kill switch failed",
        res && res.killed ? "err" : "err");
  refresh();
});

$("#btn-resume")?.addEventListener("click", async () => {
  const res = await api("/api/control/resume", { method: "POST", body: "{}" });
  toast(res ? "Kill switch cleared" : "Failed", "ok");
  refresh();
});

$("#btn-run-bt").addEventListener("click", async () => {
  const btn = $("#btn-run-bt");
  btn.innerHTML = '<span class="spin"></span> Running';
  btn.disabled = true;
  const res = await api("/api/backtest/run", { method: "POST", body: "{}" });
  if (res && res.started) {
    toast("Backtest started — results will appear when finished");
    const poll = setInterval(async () => {
      const st = await api("/api/status");
      const lb = st && st.last_backtest;
      if (lb && lb.status !== "running") {
        clearInterval(poll);
        btn.textContent = "Run Backtest";
        btn.disabled = false;
        refreshBacktest();
        toast(lb.status === "done"
          ? `Done — ${lb.trades} trades, ${lb.expectancy_R}R expectancy`
          : "Backtest failed", lb.status === "done" ? "ok" : "err");
      }
    }, 1500);
  } else {
    btn.textContent = "Run Backtest";
    btn.disabled = false;
    toast(res && res.error ? res.error : "Failed to start", "err");
  }
});

$("#btn-refresh-pos")?.addEventListener("click", () => refresh());
$("#btn-refresh-logs")?.addEventListener("click", () =>
  api("/api/logs?limit=200").then((r) => r && renderLogs(r.logs)));
$("#btn-refresh-scan")?.addEventListener("click", refreshScanner);

/* ---------------------------------------------------------- boot */
bindControlUI();
bindForwardUI();
bindDataSourceUI();
loadCommandCatalogue();
renderTicker();
// One-second refresh, plus a Server-Sent Events channel for true push updates.
// The engine writes state; the server pushes it, so the UI updates with no
// request cycle. The 1s poller is the fallback for browsers and proxies that
# buffer SSE.
const REFRESH_MS = 1000;

refresh(true);
setInterval(() => refresh(), REFRESH_MS);
setInterval(() => pollCommandOutput(), 3000);
setInterval(() => refreshScanner(), 15000);
connectStream();
if (window.location.hash === "#agents" || document.querySelector('.nav-item.active')?.dataset.view === "agents") {
  refreshAgents();
}
