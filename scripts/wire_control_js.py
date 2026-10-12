"""Wire the Control (command buttons) and Setup panels in app.js."""

from __future__ import annotations

from pathlib import Path
import sys

js_path = Path(__file__).resolve().parents[1] / "ts" / "web" / "static" / "app.js"
js = js_path.read_text(encoding="utf-8")

if "cmdGrid" in js:
    print("[skip] app.js: control UI already wired")
    sys.exit(0)

wire = r'''
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
    setSwitch($("#f-kill-risk"), r.kill_on_rism !== false || r.kill_on_risk === true);
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
'''

anchor = "/* ---------------------------------------------------------- events */"
if anchor not in js:
    print("[FAIL] app.js events anchor not found")
    sys.exit(1)
js = js.replace(anchor, wire + "\n" + anchor, 1)

# ---- nav handler: load the panel's data when its tab opens
js = js.replace(
    '''    if (view === "backtest") refreshBacktest();
    if (view === "agents") refreshAgents();''',
    '''    if (view === "backtest") refreshBacktest();
    if (view === "agents") refreshAgents();
    if (view === "control") { loadCommandCatalogue(); pollCommandOutput(true); }
    if (view === "setup") { loadBrokers(); loadSetupForms(); }''',
    1,
)

# ---- boot: bind everything
js = js.replace(
    "refresh(true);\nsetInterval(() => refresh(), 4000);",
    "bindControlUI();\nloadCommandCatalogue();\nrefresh(true);\nsetInterval(() => refresh(), 4000);\nsetInterval(() => pollCommandOutput(), 3000);",
    1,
)
js_path.write_text(js, encoding="utf-8")
print("[ok] app.js: control + setup wired")

# fix the deliberate-ish typo guard: ensure kill_on_risk reads correctly
js = js_path.read_text(encoding="utf-8")
js = js.replace("r.kill_on_rism !== false || r.kill_on_risk === true",
                "r.kill_on_risk !== false", 1)
js_path.write_text(js, encoding="utf-8")
print("[ok] fixed kill_on_risk read")
sys.exit(0)
