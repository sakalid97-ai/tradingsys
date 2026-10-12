"""Add UI for: command console (buttons/switches), broker connection,
strategy selection, instrument/timeframe selection, risk management."""

from __future__ import annotations

from pathlib import Path
import sys
import re

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"
html_path = STATIC / "index.html"
css_path = STATIC / "style.css"
js_path = STATIC / "app.js"

html = html_path.read_text(encoding="utf-8")
css = css_path.read_text(encoding="utf-8")
js = js_path.read_text(encoding="utf-8")

if 'data-view="control"' in html:
    print("[skip] already present")
    sys.exit(0)

# ----------------------------------------------------------------- 1. NAV ITEMS
old_nav = '''    <button class="nav-item" data-view="backtest">
      <span class="nav-ico">◈</span> Backtest
    </button>'''
new_nav = '''    <button class="nav-item" data-view="backtest">
      <span class="nav-ico">◈</span> Backtest
    </button>
    <button class="nav-item" data-view="control">
      <span class="nav-ico">▶</span> Control
    </button>
    <button class="nav-item" data-view="setup">
      <span class="nav-ico">⚙</span> Setup
      <span class="badge" id="badge-setup">!</span>
    </button>'''
if old_nav not in html:
    print("[FAIL] nav anchor not found")
    sys.exit(1)
html = html.replace(old_nav, new_nav, 1)
print("[ok] nav items added")

# --------------------------------------------------------- 2. THE THREE VIEWS
views = '''
    <!-- ================= CONTROL (buttons / switches / selections) ================= -->
    <section class="view" data-view="control">
      <div class="panel">
        <div class="panel-head">
          <h2>Run a Command</h2>
          <div class="panel-tools">
            <span class="pill" id="cmd-pill">idle</span>
            <button class="btn btn-ghost" id="btn-cmd-stop" title="Stop the running command">
              ■ Stop
            </button>
          </div>
        </div>
        <div class="cmd-grid" id="cmd-grid"></div>
        <p class="foot-note">Pick a card, set the options with the switches and dropdowns,
        then press RUN. Nothing is typed.</p>
      </div>

      <div class="panel" id="cmd-detail">
        <div class="panel-head">
          <h2 id="cmd-detail-name">Options</h2>
          <span class="hint" id="cmd-detail-desc"></span>
        </div>
        <div class="cmd-form" id="cmd-form"></div>
        <div class="cmd-actions">
          <button class="btn btn-primary" id="btn-cmd-run">▶ RUN</button>
          <button class="btn btn-ghost" id="btn-cmd-defaults">Reset to defaults</button>
          <button class="btn btn-ghost" id="btn-cmd-clear">Clear output</button>
        </div>
        <div class="cmd-warning hidden" id="cmd-warning"></div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>Output</h2>
          <span class="hint" id="cmd-out-meta">no command run yet</span>
        </div>
        <pre class="console-out" id="cmd-output">Select a command above to begin.</pre>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Recent Runs</h2></div>
        <div class="table-wrap">
          <table class="table" id="table-runs">
            <thead><tr><th>Command</th><th>Started</th><th>Finished</th>
              <th class="num">Exit</th><th>State</th></tr></thead>
            <tbody><tr><td colspan="5" class="empty">nothing run yet</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ================= SETUP (broker / strategy / instruments / risk) ================= -->
    <section class="view" data-view="setup">

      <div class="panel">
        <div class="panel-head"><h2>Broker Connection</h2>
          <button class="btn btn-ghost" id="btn-refresh-brokers">Refresh</button></div>
        <div class="setup-grid">
          <label class="field">
            <span class="field-label">Venue</span>
            <select id="f-broker" class="select"></select>
          </label>
          <label class="field">
            <span class="field-label">Account label</span>
            <input class="input" id="f-broker-label" placeholder="My funded account">
          </label>
          <label class="field">
            <span class="field-label">API key / login</span>
            <input class="input" id="f-broker-key" placeholder="from the environment">
          </label>
          <label class="field">
            <span class="field-label">API secret / password</span>
            <input class="input" id="f-broker-secret" type="password" placeholder="••••••">
          </label>
          <label class="field">
            <span class="field-label">Server (Exness MT5)</span>
            <input class="input" id="f-broker-passphrase" placeholder="Exness-MT5Real8">
          </label>
          <div class="field">
            <span class="field-label">Mode</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-broker-testnet" checked>
                <span class="slider"></span></label>
              <span class="switch-label" id="lbl-broker-testnet">Testnet / paper</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Store credentials on disk</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-broker-store">
                <span class="slider"></span></label>
              <span class="switch-label">off — environment variables preferred</span>
            </div>
          </div>
        </div>
        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-broker-save">Save connection</button>
          <button class="btn" id="btn-broker-test">Test connection</button>
          <button class="btn btn-ghost" id="btn-broker-disconnect">Disconnect</button>
        </div>
        <div class="broker-note" id="broker-note"></div>
        <div class="broker-test hidden" id="broker-test"></div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Strategies</h2></div>
        <div class="setup-grid">
          <div class="field">
            <span class="field-label">Trend breakout system</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-run-trend">
                <span class="slider"></span></label>
              <span class="switch-label">20-day breakout with regime filter</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Multi-agent system</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-run-agents">
                <span class="slider"></span></label>
              <span class="switch-label">six agents + Bayesian + Kelly</span>
            </div>
          </div>
          <label class="field">
            <span class="field-label">Entry lookback (bars)</span>
            <input class="input num" type="number" id="f-entry-lookback" value="20">
          </label>
          <label class="field">
            <span class="field-label">Exit lookback (bars)</span>
            <input class="input num" type="number" id="f-exit-lookback" value="10">
          </label>
          <label class="field">
            <span class="field-label">Regime MA period</span>
            <input class="input num" type="number" id="f-regime-ma" value="200">
          </label>
          <label class="field">
            <span class="field-label">Minimum ADX</span>
            <input class="input num" type="number" id="f-adx-min" value="20" step="1">
          </label>
          <label class="field">
            <span class="field-label">Initial stop (ATR ×)</span>
            <input class="input num" type="number" id="f-atr-stop" value="2" step="0.1">
          </label>
          <label class="field">
            <span class="field-label">Trailing stop (ATR ×)</span>
            <input class="input num" type="number" id="f-atr-trail" value="3" step="0.1">
          </label>
          <label class="field">
            <span class="field-label">Kelly fraction</span>
            <input class="input num" type="number" id="f-kelly" value="0.25" step="0.05">
          </label>
          <label class="field">
            <span class="field-label">Minimum expectancy (R)</span>
            <input class="input num" type="number" id="f-min-exp" value="0.2" step="0.05">
          </label>
          <label class="field">
            <span class="field-label">Minimum confidence</span>
            <input class="input num" type="number" id="f-min-conf" value="0.65" step="0.05">
          </label>
          <div class="field">
            <span class="field-label">Allow short entries</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-shorts">
                <span class="slider"></span></label>
              <span class="switch-label">two-sided trading</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Require above-average volume</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-volume">
                <span class="slider"></span></label>
              <span class="switch-label">1m volume is unreliable — leave off</span>
            </div>
          </div>
        </div>
        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-save-strategy">Save strategy</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Instruments &amp; Timeframe</h2></div>
        <div class="setup-grid">
          <label class="field">
            <span class="field-label">Timeframe</span>
            <select id="f-timeframe" class="select">
              <option value="1m">1 minute</option>
              <option value="5m">5 minutes</option>
              <option value="15m">15 minutes</option>
              <option value="30m">30 minutes</option>
              <option value="1h">1 hour</option>
              <option value="1d">1 day</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">History range</span>
            <select id="f-range" class="select">
              <option value="7d">7 days</option>
              <option value="60d">60 days</option>
              <option value="1y">1 year</option>
              <option value="5y">5 years</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">Max symbols</span>
            <input class="input num" type="number" id="f-max-symbols" value="20">
          </label>
          <label class="field">
            <span class="field-label">Market gate symbol</span>
            <input class="input" id="f-gate" value="SPY" placeholder="SPY">
          </label>
          <div class="field">
            <span class="field-label">Asset classes</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-class-index">
                <span class="slider"></span></label>
              <span class="switch-label">Indices</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Forex</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-class-forex">
                <span class="slider"></span></label>
              <span class="switch-label">FX majors</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Crypto</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-class-crypto">
                <span class="slider"></span></label>
              <span class="switch-label">24/7 markets</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Use the market gate</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-use-gate" checked>
                <span class="slider"></span></label>
              <span class="switch-label">no new entries when risk-off</span>
            </div>
          </div>
        </div>
        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-save-instruments">Save instruments</button>
          <button class="btn btn-ghost" id="btn-fill-defaults">Use class defaults</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Position Sizing &amp; Risk</h2>
          <button class="btn btn-ghost" id="btn-refresh-prop">Refresh</button></div>
        <div class="setup-grid">
          <label class="field">
            <span class="field-label">Prop-firm profile</span>
            <select id="f-prop" class="select"></select>
          </label>
          <label class="field">
            <span class="field-label">Starting balance</span>
            <input class="input num" type="number" id="f-start-balance" value="100000">
          </label>
          <label class="field">
            <span class="field-label">Safety buffer</span>
            <select id="f-buffer" class="select">
              <option value="0">0% — exactly the firm's limit (not recommended)</option>
              <option value="0.15" selected>15% inside the limit</option>
              <option value="0.25">25% inside (recommended)</option>
              <option value="0.35">35% inside</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">Risk per trade</span>
            <input class="input num" type="number" id="f-risk-trade" value="0.01" step="0.005">
          </label>
          <label class="field">
            <span class="field-label">Max position size</span>
            <input class="input num" type="number" id="f-max-pos-pct" value="0.2" step="0.05">
          </label>
          <label class="field">
            <span class="field-label">Max open positions</span>
            <input class="input num" type="number" id="f-max-pos" value="8">
          </label>
          <label class="field">
            <span class="field-label">Max total risk</span>
            <input class="input num" type="number" id="f-max-risk" value="0.06" step="0.01">
          </label>
          <label class="field">
            <span class="field-label">Daily loss limit</span>
            <input class="input num" type="number" id="f-daily" value="0.03" step="0.005">
          </label>
          <label class="field">
            <span class="field-label">Drawdown halt</span>
            <input class="input num" type="number" id="f-dd" value="0.15" step="0.01">
          </label>
          <label class="field">
            <span class="field-label">Max fraction of bankroll</span>
            <input class="input num" type="number" id="f-max-frac" value="0.05" step="0.01">
          </label>
          <label class="field">
            <span class="field-label">Slippage (bps)</span>
            <input class="input num" type="number" id="f-slippage" value="5" step="1">
          </label>
          <label class="field">
            <span class="field-label">Commission</span>
            <input class="input num" type="number" id="f-commission" value="0.005" step="0.005">
          </label>
          <div class="field">
            <span class="field-label">Dry run (never submit)</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-dry-run" checked>
                <span class="slider"></span></label>
              <span class="switch-label">no orders are sent</span>
            </div>
          </div>
          <div class="field">
            <span class="field-label">Auto kill switch on breach</span>
            <div class="switch-row">
              <label class="switch"><input type="checkbox" id="f-kill-risk" checked>
                <span class="slider"></span></label>
              <span class="switch-label">halt and flatten automatically</span>
            </div>
          </div>
        </div>
        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-apply-prop">Apply prop-firm profile</button>
          <button class="btn" id="btn-save-risk">Save risk settings</button>
        </div>
        <div class="prop-note" id="prop-note"></div>
      </div>
    </section>
'''

anchor = "    <!-- ================= LOGS ================= -->"
if anchor not in html:
    print("[FAIL] LOGS anchor not found")
    sys.exit(1)
html = html.replace(anchor, views + "\n" + anchor, 1)
html_path.write_text(html, encoding="utf-8")
print("[ok] index.html: control + setup views added")

# ---------------------------------------------------------------------- 3. CSS
css += '''

/* ============================================================
   Control panel: command cards, switch rows, selectors
   ============================================================ */
.cmd-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 12px; padding: 16px;
}
.cmd-card {
  text-align: left; cursor: pointer; padding: 14px;
  background: linear-gradient(160deg, var(--panel-2), var(--bg-2));
  border: 1px solid var(--line); border-radius: 10px;
  color: var(--text); font-family: var(--sans); transition: .15s ease;
}
.cmd-card:hover { border-color: var(--cyan); transform: translateY(-2px); }
.cmd-card.active {
  border-color: var(--green);
  box-shadow: 0 0 0 1px rgba(52,211,153,.35) inset;
}
.cmd-card .c-title { font-size: 12.5px; font-weight: 700; }
.cmd-card .c-desc {
  font-size: 10.5px; color: var(--text-faint); margin-top: 4px; line-height: 1.45;
}

.cmd-form {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px 18px; padding: 16px;
}
.cmd-actions {
  display: flex; gap: 10px; padding: 0 16px 16px; flex-wrap: wrap;
}
.cmd-warning {
  margin: 0 16px 16px; padding: 11px 14px; border-radius: 9px;
  border: 1px solid rgba(251,191,36,.4); background: rgba(251,191,36,.1);
  color: #fde68a; font-size: 11.5px; line-height: 1.5;
}
.cmd-warning.hidden { display: none; }

.console-out {
  font-family: var(--mono); font-size: 11px; line-height: 1.55;
  padding: 14px 16px; margin: 0; max-height: 420px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; color: var(--text-dim);
  background: #060a0e;
}

/* ------------------------------------------------ form controls: no typing */
.setup-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px 18px; padding: 16px;
}
.field { display: flex; flex-direction: column; gap: 6px; }
.field-label {
  font-size: 10px; letter-spacing: .13em; color: var(--text-faint);
  text-transform: uppercase; font-weight: 700;
}

.select, .input {
  font-family: var(--sans); font-size: 12px; padding: 9px 11px;
  border-radius: 8px; background: var(--panel-2); color: var(--text);
  border: 1px solid var(--line); cursor: pointer; width: 100%;
}
.input.num { font-family: var(--mono); }
.select:hover, .input:hover { border-color: #2b3d50; }
.select:focus, .input:focus { outline: none; border-color: var(--cyan); }
input.input[type="password"] { letter-spacing: .14em; }

.switch-row { display: flex; align-items: center; gap: 10px; }
.switch { position: relative; display: inline-block; width: 42px; height: 22px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; inset: 0; cursor: pointer; border-radius: 999px;
  background: #1c2836; border: 1px solid var(--line); transition: .18s ease;
}
.slider::before {
  content: ""; position: absolute; height: 14px; width: 14px; left: 3px; top: 3px;
  border-radius: 50%; background: #64748b; transition: .18s ease;
}
.switch input:checked + .slider { background: rgba(52,211,153,.22); border-color: var(--green); }
.switch input:checked + .slider::before { transform: translateX(20px); background: var(--green); }
.switch-label { font-size: 11px; color: var(--text-dim); }

.setup-actions { display: flex; gap: 10px; padding: 0 16px 16px; flex-wrap: wrap; }
.broker-note, .prop-note {
  margin: 0 16px 16px; padding: 11px 14px; border-radius: 9px; font-size: 11.5px;
  line-height: 1.55; border: 1px solid var(--line); background: var(--panel-2);
  color: var(--text-dim);
}
.broker-note:empty, .prop-note:empty { display: none; }
.broker-test {
  margin: 0 16px 16px; padding: 12px 14px; border-radius: 9px; font-size: 11.5px;
  border: 1px solid var(--line); background: var(--panel-2); line-height: 1.6;
}
.broker-test.hidden { display: none; }
.broker-test.ok { border-color: rgba(52,211,153,.4); }
.broker-test.bad { border-color: rgba(248,113,113,.4); }
.broker-test .bt-line { margin: 2px 0; }
'''
css_path.write_text(css, encoding="utf-8")
print("[ok] style.css: control + setup styles added")

js_path.write_text(js, encoding="utf-8")
sys.exit(0)
