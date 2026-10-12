"""Add the data-source selector to the Setup panel and wire its JS."""

from __future__ import annotations

import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"

html_path = STATIC / "index.html"
html = html_path.read_text(encoding="utf-8")

if "f-data-source" in html:
    print("[skip] selector already present")
    sys.exit(0)

old = '''          <label class="field">
            <span class="field-label">Timeframe</span>
            <select id="f-timeframe" class="select">'''
new = '''          <label class="field">
            <span class="field-label">Data source</span>
            <select id="f-data-source" class="select"></select>
          </label>
          <label class="field">
            <span class="field-label">Timeframe</span>
            <select id="f-timeframe" class="select">'''
if old not in html:
    print("[FAIL] timeframe field anchor not found")
    sys.exit(1)
html = html.replace(old, new, 1)

# a note under the instruments panel explaining what each source covers
old_actions = '''        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-save-instruments">Save instruments</button>
          <button class="btn btn-ghost" id="btn-fill-defaults">Use class defaults</button>
        </div>'''
new_actions = '''        <div class="broker-note" id="data-source-note"></div>
        <div class="setup-actions">
          <button class="btn btn-primary" id="btn-save-instruments">Save instruments</button>
          <button class="btn btn-ghost" id="btn-fill-defaults">Use class defaults</button>
        </div>'''
if old_actions not in html:
    print("[FAIL] instruments actions anchor not found")
    sys.exit(1)
html = html.replace(old_actions, new_actions, 1)
html_path.write_text(html, encoding="utf-8")
print("[ok] index.html: data-source selector added")

# ------------------------------------------------------------------- the JS
js_path = STATIC / "app.js"
js = js_path.read_text(encoding="utf-8")

if "loadDataSources" in js:
    print("[skip] js already wired")
    sys.exit(0)

code = r'''
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
'''

anchor = "/* ---------------------------------------------------------- polling */"
if anchor not in js:
    print("[FAIL] polling anchor not found")
    sys.exit(1)
js = js.replace(anchor, code + "\n" + anchor, 1)

# load it when the setup tab opens
js = js.replace(
    '''    if (view === "setup") { loadBrokers(); loadSetupForms(); }''',
    '''    if (view === "setup") { loadBrokers(); loadSetupForms(); loadDataSources(); }''',
    1,
)
# save it with the rest of the instruments block
js = js.replace(
    '''  return {
    timeframe: $("#f-timeframe").value,
    range_: $("#f-range").value,''',
    '''  return {
    data_source: $("#f-data-source") ? $("#f-data-source").value : "router",
    timeframe: $("#f-timeframe").value,
    range_: $("#f-range").value,''',
    1,
)
# and restore it
js = js.replace(
    '''    const i = settings.instruments || {};
    if (i.timeframe) $("#f-timeframe").value = i.timeframe;''',
    '''    const i = settings.instruments || {};
    if (i.data_source && $("#f-data-source")) {
      $("#f-data-source").value = i.data_source;
      updateDataSourceNote();
    }
    if (i.timeframe) $("#f-timeframe").value = i.timeframe;''',
    1,
)
js = js.replace(
    "bindControlUI();\nbindForwardUI();",
    "bindControlUI();\nbindForwardUI();\nbindDataSourceUI();",
    1,
)
js_path.write_text(js, encoding="utf-8")
print("[ok] app.js: data-source selector wired")
sys.exit(0)
