"""Add START / STOP engine buttons to the dashboard and wire them in app.js."""

from pathlib import Path
import sys

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"

# ------------------------------------------------------------------- HTML
html_path = STATIC / "index.html"
html = html_path.read_text(encoding="utf-8")

if "btn-start-engine" in html:
    print("[skip] index.html: engine buttons already present")
else:
    old = '''    <span class="pill" id="pill-mode">IDLE</span>
    <button class="btn btn-danger" id="btn-kill" title="Emergency stop — flatten and halt">
      KILL SWITCH
    </button>
    <button class="btn btn-ghost" id="btn-resume" title="Clear the kill switch and resume">
      RESUME
    </button>'''
    new = '''    <span class="pill" id="pill-mode">IDLE</span>
    <label class="engine-mode" title="What the engine will trade">
      <select id="engine-mode">
        <option value="agents">Multi-agent (indices / forex / crypto)</option>
        <option value="paper">Trend system (paper broker)</option>
      </select>
    </label>
    <button class="btn btn-start" id="btn-start-engine" title="Start the engine in this process">
      ▶ START ENGINE
    </button>
    <button class="btn btn-ghost hidden" id="btn-stop-engine" title="Stop the engine">
      ■ STOP ENGINE
    </button>
    <button class="btn btn-danger" id="btn-kill" title="Emergency stop — flatten and halt">
      KILL SWITCH
    </button>
    <button class="btn btn-ghost" id="btn-resume" title="Clear the kill switch and resume">
      RESUME
    </button>'''
    if old not in html:
        print("[FAIL] index.html: topbar anchor not found")
        sys.exit(1)
    html = html.replace(old, new, 1)
    html_path.write_text(html, encoding="utf-8")
    print("[ok]   index.html: START / STOP engine buttons added")

# --------------------------------------------------------------------- CSS
css_path = STATIC / "style.css"
css = css_path.read_text(encoding="utf-8")

if ".btn-start" not in css:
    css += '''
/* ------------------------------------------------ engine control */
.btn-start {
  color: #04140e; background: var(--green); border-color: var(--green);
  font-weight: 800; letter-spacing: .08em;
}
.btn-start:hover { background: #4ade9f; transform: translateY(-1px); }
.btn-start:disabled { opacity: .55; cursor: not-allowed; transform: none; }

.hidden { display: none !important; }

select.engine-mode {
  font-family: var(--sans); font-size: 11px; font-weight: 600;
  padding: 7px 10px; border-radius: 8px;
  background: var(--panel-2); color: var(--text);
  border: 1px solid var(--line); cursor: pointer; max-width: 230px;
}
select.engine-mode:hover { border-color: #2b3d50; }
'''
    css_path.write_text(css, encoding="utf-8")
    print("[ok]   style.css: engine control styles added")
else:
    print("[skip] style.css: engine control styles already present")

# --------------------------------------------------------------------- JS
js_path = STATIC / "app.js"
js = js_path.read_text(encoding="utf-8")

if "btn-start-engine" in js:
    print("[skip] app.js: engine buttons already wired")
    sys.exit(0)

wire = '''
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
'''

anchor = '/* ---------------------------------------------------------- events */'
if anchor not in js:
    print("[FAIL] app.js: events anchor not found")
    sys.exit(1)
js = js.replace(anchor, wire + "\n" + anchor, 1)

# --- the nav handler also needs to know about the new buttons
js = js.replace(
    '''$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {''',
    '''$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {''',
    1,
)

# --- renderStatus should reflect the real running state
old_pill = '''  const pill = $("#pill-mode");
  pill.textContent = status.killed ? "KILLED" : status.engine_running ? mode : "STOPPED";
  pill.className = "pill " +
    (status.killed || !status.engine_running ? "stopped" : mode.toLowerCase());'''
new_pill = '''  const pill = $("#pill-mode");
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
  }'''
if old_pill in js:
    js = js.replace(old_pill, new_pill, 1)
    print("[ok]   app.js: renderStatus reflects running state")
else:
    print("[warn] app.js: renderStatus pill block not found")

js_path.write_text(js, encoding="utf-8")
print("[ok]   app.js: START / STOP handlers wired")
sys.exit(0)
