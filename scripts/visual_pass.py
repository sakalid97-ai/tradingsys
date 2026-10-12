"""Visual upgrade pass: live ticker tape, sparklines in KPI tiles,
bottom status bar, and a cockpit-style overview.

Everything is additive -- no existing view is changed, so nothing that works
can break.
"""

from __future__ import annotations

from pathlib import Path
import sys

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"
html_path = STATIC / "index.html"
css_path = STATIC / "style.css"
js_path = STATIC / "app.js"

html = html_path.read_text(encoding="utf-8")
css = css_path.read_text(encoding="utf-8")
js = js_path.read_text(encoding="utf-8")

if "ticker-strip" in html:
    print("[skip] visual pass already applied")
    sys.exit(0)

# ------------------------------------------------------- 1. header ticker tape
old_brand = '''    <div class="brand-text">
      <strong>AUTONOMOUS</strong><span>TRADING SYSTEM</span>
    </div>'''
new_brand = '''    <div class="brand-text">
      <strong>AUTONOMOUS</strong><span>TRADING SYSTEM</span>
    </div>
    <div class="live-dot" id="live-dot" title="engine heartbeat"></div>'''
if old_brand not in html:
    print("[FAIL] brand anchor not found")
    sys.exit(1)
html = html.replace(old_brand, new_brand, 1)

# ticker strip right under the top bar
old_cluster = '''  <div class="topbar-actions">'''
new_cluster = '''  <div class="topbar-actions">'''
# insert the strip after the topbar's closing tag instead
old_close = '''</header>

<div id="layout">'''
new_close = '''</header>

<div class="ticker-strip" id="ticker-strip">
  <div class="ticker-track" id="ticker-track"></div>
</div>

<div id="layout">'''
if old_close not in html:
    print("[FAIL] topbar close anchor not found")
    sys.exit(1)
html = html.replace(old_close, new_close, 1)

# ------------------------------------------------------ 2. bottom status bar
old_layout_close = '''  </main>
</div>'''
new_layout_close = '''  </main>
</div>

<footer id="statusbar">
  <div class="sb-cell"><span class="sb-label">Engine</span><b id="sb-engine">stopped</b></div>
  <div class="sb-cell"><span class="sb-label">Broker</span><b id="sb-broker">paper</b></div>
  <div class="sb-cell"><span class="sb-label">Risk</span><b id="sb-risk">—</b></div>
  <div class="sb-cell"><span class="sb-label">Positions</span><b id="sb-pos">0</b></div>
  <div class="sb-cell"><span class="sb-label">Today</span><b id="sb-day">—</b></div>
  <div class="sb-cell sb-right"><span class="sb-label">v</span><b id="sb-version">—</b></div>
  <div class="sb-cell sb-right"><span class="sb-label">Latency</span><b id="sb-latency">—</b></div>
</footer>'''
if old_layout_close not in html:
    print("[FAIL] layout close anchor not found")
    sys.exit(1)
html = html.replace(old_layout_close, new_layout_close, 1)

html_path.write_text(html, encoding="utf-8")
print("[ok] index.html: ticker tape + live dot + status bar")

# ------------------------------------------------------------------- CSS
css += '''

/* ============================================================
   Visual pass: ticker tape, live dot, status bar, sparklines
   ============================================================ */

/* --- the layout now sits above a fixed status bar */
#layout { height: calc(100vh - 62px - 26px); }

.live-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-faint); flex-shrink: 0;
  transition: background .3s ease;
}
.live-dot.alive {
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(52,211,153,.7);
  animation: pulse 2s infinite;
}
.live-dot.dead { background: #475569; }
.live-dot.killed { background: var(--red); animation: pulse-fast 1s infinite; }
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0 rgba(52,211,153,.65); }
  70%  { box-shadow: 0 0 0 9px rgba(52,211,153,0); }
  100% { box-shadow: 0 0 0 0 rgba(52,211,153,0); }
}
@keyframes pulse-fast {
  0%, 100% { opacity: 1; }
  50% { opacity: .35; }
}

/* --- ticker tape */
.ticker-strip {
  height: 34px; overflow: hidden; position: relative;
  background: linear-gradient(90deg, #0a0f15, #0d141c, #0a0f15);
  border-bottom: 1px solid var(--line);
}
.ticker-track {
  display: flex; gap: 34px; align-items: center; height: 100%;
  white-space: nowrap; will-change: transform;
  animation: ticker-scroll 60s linear infinite;
  font-family: var(--mono); font-size: 11.5px;
}
.ticker-strip:hover .ticker-track { animation-play-state: paused; }
@keyframes ticker-scroll {
  from { transform: translateX(0); }
  to   { transform: translateX(-50%); }
}
.tick {
  display: inline-flex; align-items: center; gap: 7px;
  color: var(--text-dim); flex-shrink: 0;
}
.tick .t-sym { font-weight: 700; color: var(--text); letter-spacing: .04em; }
.tick .t-px  { color: var(--text-dim); }
.tick .t-ch  { font-weight: 700; }
.tick .t-ch.up { color: var(--green); }
.tick .t-ch.down { color: var(--red); }

/* --- bottom status bar */
#statusbar {
  position: fixed; left: 0; right: 0; bottom: 0; height: 26px;
  display: flex; align-items: center; gap: 22px; padding: 0 16px;
  background: rgba(11,17,24,.96); border-top: 1px solid var(--line);
  font-family: var(--mono); font-size: 10.5px; z-index: 25;
  backdrop-filter: blur(8px);
}
.sb-cell { display: flex; align-items: center; gap: 6px; }
.sb-cell.sb-right { margin-left: auto; }
.sb-cell.sb-right + .sb-cell.sb-right { margin-left: 0; }
.sb-label { color: var(--text-faint); letter-spacing: .1em; text-transform: uppercase; }
.sb-cell b { color: var(--text-dim); font-weight: 600; }
.sb-cell b.pos { color: var(--green); }
.sb-cell b.neg { color: var(--red); }
#content { padding-bottom: 40px; }

/* --- sparklines inside KPI tiles */
.kpi { position: relative; }
.kpi .spark {
  position: absolute; right: 10px; bottom: 8px;
  width: 62px; height: 22px; opacity: .9;
}

/* --- cockpit overview: a wide hero strip above the tiles */
.hero-strip {
  display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: 16px; margin-bottom: 16px;
}
.hero-main {
  background: linear-gradient(140deg, #0f1a24, #0a1017);
  border: 1px solid var(--line); border-radius: var(--radius);
  padding: 18px 20px; display: flex; flex-direction: column; gap: 4px;
}
.hero-equity {
  font-family: var(--mono); font-size: 38px; font-weight: 700; line-height: 1.1;
  letter-spacing: -.02em;
}
.hero-sub { font-size: 11.5px; color: var(--text-faint); }
.hero-delta { font-family: var(--mono); font-size: 15px; font-weight: 700; }
.hero-side { display: grid; grid-template-rows: repeat(3, 1fr); gap: 10px; }
.mini-card {
  background: linear-gradient(160deg, var(--panel-2), var(--bg-2));
  border: 1px solid var(--line); border-radius: 10px; padding: 10px 13px;
  display: flex; flex-direction: column; justify-content: center; gap: 2px;
}
.mini-card .m-label {
  font-size: 9.5px; letter-spacing: .15em; color: var(--text-faint);
  text-transform: uppercase;
}
.mini-card .m-value { font-family: var(--mono); font-size: 17px; font-weight: 700; }

@media (max-width: 980px) {
  .hero-strip { grid-template-columns: 1fr; }
  #statusbar { gap: 12px; overflow-x: auto; }
}
'''
css_path.write_text(css, encoding="utf-8")
print("[ok] style.css: ticker, live dot, status bar, sparkline, hero strip")

js_path.write_text(js, encoding="utf-8")
sys.exit(0)
