"""Wire the Forward view's JS: render drift table, chart and verdict."""

from __future__ import annotations

from pathlib import Path
import sys

js_path = Path(__file__).resolve().parents[1] / "ts" / "web" / "static" / "app.js"
js = js_path.read_text(encoding="utf-8")

if "renderForward" in js:
    print("[skip] forward JS already wired")
    sys.exit(0)

code = r'''
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
'''

anchor = "/* ---------------------------------------------------------- polling */"
if anchor not in js:
    print("[FAIL] polling anchor not found")
    sys.exit(1)
js = js.replace(anchor, code + "\n" + anchor, 1)

# nav handler + boot
js = js.replace(
    '''    if (view === "control") { loadCommandCatalogue(); pollCommandOutput(true); }''',
    '''    if (view === "control") { loadCommandCatalogue(); pollCommandOutput(true); }
    if (view === "forward") loadForward();''',
    1,
)
js = js.replace(
    "bindControlUI();\nloadCommandCatalogue();",
    "bindControlUI();\nbindForwardUI();\nloadCommandCatalogue();",
    1,
)
js_path.write_text(js, encoding="utf-8")
print("[ok] app.js: forward view wired")
sys.exit(0)
