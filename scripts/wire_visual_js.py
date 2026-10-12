"""Wire the ticker tape, sparklines, live dot and status bar."""

from __future__ import annotations

from pathlib import Path
import sys

js_path = Path(__file__).resolve().parents[1] / "ts" / "web" / "static" / "app.js"
js = js_path.read_text(encoding="utf-8")

if "drawSparkline" in js:
    print("[skip] sparkline wiring already present")
    sys.exit(0)

code = r'''
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
'''

anchor = "/* ---------------------------------------------------------- polling */"
if anchor not in js:
    print("[FAIL] polling anchor not found")
    sys.exit(1)
js = js.replace(anchor, code + "\n" + anchor, 1)

# refresh() should also feed the ticker + status bar
old_refresh = '''async function refresh(scannerToo = false) {
  const [status, snap, metrics, trades, logs] = await Promise.all([
    api("/api/status"),
    api("/api/snapshot"),
    api("/api/metrics"),
    api("/api/trades?limit=400"),
    api("/api/logs?limit=120"),
  ]);

  if (status && snap) {
    renderStatus(status, snap);'''
new_refresh = '''async function refresh(scannerToo = false) {
  const [status, snap, metrics, trades, logs] = await Promise.all([
    api("/api/status"),
    api("/api/snapshot"),
    api("/api/metrics"),
    api("/api/trades?limit=400"),
    api("/api/logs?limit=120"),
  ]);

  if (status && snap) {
    renderStatus(status, snap);
    updateStatusBar(status, snap);'''
if old_refresh not in js:
    print("[FAIL] refresh anchor not found")
    sys.exit(1)
js = js.replace(old_refresh, new_refresh, 1)

# harvest ticker prices whenever the agents payload arrives
js = js.replace(
    '''async function refreshAgents() {
  const data = await api("/api/agents");
  if (!data) return;''',
    '''async function refreshAgents() {
  const data = await api("/api/agents");
  if (data) harvestTickerPrices(data);
  renderTicker();
  if (!data) return;''',
    1,
)

# render the ticker on boot too, so the strip is not blank
js = js.replace(
    "bindControlUI();\nloadCommandCatalogue();",
    "bindControlUI();\nloadCommandCatalogue();\nrenderTicker();",
    1,
)
js_path.write_text(js, encoding="utf-8")
print("[ok] app.js: ticker, sparkline, live dot, status bar wired")
sys.exit(0)
