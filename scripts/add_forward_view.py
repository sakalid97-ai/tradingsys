"""Add the Forward Tracking view to the dashboard."""

from __future__ import annotations

from pathlib import Path
import sys

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"
html_path = STATIC / "index.html"
css_path = STATIC / "style.css"
js_path = STATIC / "app.js"

html = html_path.read_text(encoding="utf-8")

if 'data-view="forward"' in html:
    print("[skip] forward view already present")
    sys.exit(0)

# --- nav item
old_nav = '''    <button class="nav-item" data-view="setup">
      <span class="nav-ico">⚙</span> Setup
      <span class="badge" id="badge-setup">!</span>
    </button>'''
new_nav = '''    <button class="nav-item" data-view="forward">
      <span class="nav-ico">◷</span> Forward
      <span class="badge" id="badge-forward">0</span>
    </button>
    <button class="nav-item" data-view="setup">
      <span class="nav-ico">⚙</span> Setup
      <span class="badge" id="badge-setup">!</span>
    </button>'''
if old_nav not in html:
    print("[FAIL] nav anchor not found")
    sys.exit(1)
html = html.replace(old_nav, new_nav, 1)

# --- the view itself
view = '''
    <!-- ================= FORWARD TRACKING ================= -->
    <section class="view" data-view="forward">
      <div class="verdict" id="forward-verdict">no forward trades recorded yet</div>

      <div class="setup-actions" style="padding:0 0 14px 0">
        <button class="btn btn-primary" id="btn-fwd-baseline">Use last backtest as the prediction</button>
        <button class="btn" id="btn-fwd-import">Import engine trades</button>
        <button class="btn btn-ghost" id="btn-fwd-refresh">Refresh</button>
        <button class="btn btn-ghost" id="btn-fwd-reset">Reset journal</button>
      </div>

      <div class="kpi-grid" id="forward-kpis"></div>

      <div class="panel">
        <div class="panel-head"><h2>Forward P&amp;L</h2>
          <span class="hint" id="fwd-curve-meta"></span></div>
        <div class="chart-box"><canvas id="chart-forward"></canvas></div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Predicted vs Realised</h2>
          <span class="hint">the only honest comparison: is the gap bigger than noise?</span></div>
        <div class="table-wrap">
          <table class="table" id="table-drift">
            <thead><tr><th>Metric</th><th class="num">Backtest predicted</th>
              <th class="num">Realised</th><th>95% interval (realised)</th>
              <th class="num">Gap</th><th>Verdict</th></tr></thead>
            <tbody><tr><td colspan="6" class="empty">capture a baseline to compare</td></tr></tbody>
          </table>
        </div>
        <p class="foot-note">A metric is only flagged when the <b>prediction</b> falls
        outside the interval the realised sample can support — that is the definition of
        a difference larger than sampling noise.</p>
      </div>

      <div class="panel-row">
        <div class="panel">
          <div class="panel-head"><h2>By Strategy</h2></div>
          <div class="table-wrap">
            <table class="table" id="table-fwd-strategy">
              <thead><tr><th>Strategy</th><th class="num">Trades</th>
                <th class="num">Win %</th><th class="num">Total R</th>
                <th class="num">P&amp;L</th></tr></thead>
              <tbody><tr><td colspan="5" class="empty">—</td></tr></tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>By Symbol</h2></div>
          <div class="table-wrap">
            <table class="table" id="table-fwd-symbol">
              <thead><tr><th>Symbol</th><th class="num">Trades</th>
                <th class="num">Win %</th><th class="num">Total R</th>
                <th class="num">P&amp;L</th></tr></thead>
              <tbody><tr><td colspan="5" class="empty">—</td></tr></tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
'''

anchor = "    <!-- ================= SETUP (broker / strategy / instruments / risk) ================= -->"
if anchor not in html:
    print("[FAIL] setup view anchor not found")
    sys.exit(1)
html = html.replace(anchor, view + "\n" + anchor, 1)
html_path.write_text(html, encoding="utf-8")
print("[ok] index.html: forward view added")

# ------------------------------------------------------------------- CSS
css = css_path.read_text(encoding="utf-8")
css += '''

/* ------------------------------------------------ forward tracking */
.verdict {
  padding: 14px 18px; border-radius: 10px; margin-bottom: 14px;
  border: 1px solid var(--line); border-left-width: 4px;
  background: var(--panel-2); font-size: 12.5px; line-height: 1.6;
  font-family: var(--mono);
}
.verdict.tracking { border-left-color: var(--green); color: #a7f3d0; }
.verdict.early    { border-left-color: var(--cyan); color: #bae6fd; }
.verdict.partial  { border-left-color: var(--amber); color: #fde68a; }
.verdict.drifting { border-left-color: var(--red); color: #fecaca; }
.verdict.none     { border-left-color: var(--text-faint); color: var(--text-dim); }

.verdict-class {
  display: inline-block; font-size: 10px; font-weight: 800; letter-spacing: .1em;
  padding: 2px 8px; border-radius: 999px; margin-right: 10px; text-transform: uppercase;
}
.verdict.tracking .verdict-class { background: rgba(52,211,153,.18); color: var(--green); }
.verdict.early .verdict-class    { background: rgba(56,189,248,.18); color: var(--cyan); }
.verdict.partial .verdict-class  { background: rgba(251,191,36,.18); color: var(--amber); }
.verdict.drifting .verdict-class { background: rgba(248,113,113,.18); color: var(--red); }
.verdict.none .verdict-class     { background: var(--panel); color: var(--text-faint); }
'''
css_path.write_text(css, encoding="utf-8")
print("[ok] style.css: forward styles added")

js_path.write_text(js_path.read_text(encoding="utf-8"), encoding="utf-8")
sys.exit(0)
