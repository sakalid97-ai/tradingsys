"""Turn the Overview into a cockpit: hero equity strip + KPI sparklines."""

from __future__ import annotations

from pathlib import Path
import sys

js_path = Path(__file__).resolve().parents[1] / "ts" / "web" / "static" / "app.js"
js = js_path.read_text(encoding="utf-8")

if "hero-strip" in js:
    print("[skip] hero strip already present")
    sys.exit(0)

# --- extend kpiTile with an optional sparkline
old_tile = '''function kpiTile(label, value, sub, cls = "neu", accent = "#1d2a37") {
  return `<div class="kpi" style="--accent:${accent}">
      <div class="k-label">${esc(label)}</div>
      <div class="k-value ${cls}">${value}</div>
      ${sub ? `<div class="k-sub">${esc(sub)}</div>` : ""}
    </div>`;
}'''
new_tile = '''function kpiTile(label, value, sub, cls = "neu", accent = "#1d2a37", spark = null) {
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
}'''
if old_tile not in js:
    print("[FAIL] kpiTile anchor not found")
    sys.exit(1)
js = js.replace(old_tile, new_tile, 1)
print("[ok] kpiTile + hero strip helpers")

# --- renderOverview: use the hero strip and pass sparkline data
old_head = '''  const tiles = [
    kpiTile("Equity", fmtMoney(equity), snap.mode ? `mode: ${snap.mode}` : "",
            "neu", equity ? "#34d399" : "#1d2a37"),'''
new_head = '''  const eqSeries = (snap.equity_series || []).filter((r) => r && r.equity != null);
  const eqValues = eqSeries.map((r) => r.equity);

  const tiles = [
    kpiTile("Total Return", fmtPct(bt.total_return ?? snap.total_return_pct ?? null),
            `CAGR ${fmtPct(bt.cagr ?? null)}`,
            moneyClass((bt.total_return ?? snap.total_return_pct) ?? 0), "#a78bfa",
            eqValues),'''
if old_head not in js:
    print("[FAIL] renderOverview head anchor not found")
    sys.exit(1)
js = js.replace(old_head, new_head, 1)
print("[ok] renderOverview: equity tile now carries a sparkline")

# drop the old duplicate Total Return tile further down the list
old_dup = '''    kpiTile("Total Return", fmtPct(bt.total_return ?? snap.total_return_pct ?? null),
            `CAGR ${fmtPct(bt.cagr ?? null)}`, moneyClass((bt.total_return ?? snap.total_return_pct) ?? 0), "#a78bfa"),
'''
if old_dup in js:
    js = js.replace(old_dup, "", 1)
    print("[ok] removed the duplicate Total Return tile")

# wrap the grid: hero strip + tiles, then close the grid
old_render = '''  $("#kpi-grid").innerHTML = tiles.join("");'''
new_render = '''  const heroHtml = heroStrip(snap, metrics, eqSeries);
  const grid = $("#kpi-grid");
  grid.className = "hero-tiles";
  grid.innerHTML = heroHtml + tiles.join("") + "</div>";
  drawAllSparklines();'''
if old_render not in js:
    print("[FAIL] kpi-grid render anchor not found")
    sys.exit(1)
js = js.replace(old_render, new_render, 1)
print("[ok] hero strip rendered into the overview")

js_path.write_text(js, encoding="utf-8")
sys.exit(0)
