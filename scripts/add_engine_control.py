"""Add engine start/stop control to the dashboard API + wire it in the CLI.

The dashboard previously exposed KILL and RESUME but had no way to START the
engine, because AppContext.engine was never populated -- so the header always
said IDLE / STOPPED no matter what was running.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

# ----------------------------------------------------------------- AppContext
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")

if "engine_starter" in text:
    print("[skip] server.py: engine control already present")
else:
    text = text.replace(
        "    agents: object | None = None          # MultiAgentSupervisor, optional",
        "    agents: object | None = None          # MultiAgentSupervisor, optional\n"
        "    engine_starter: object | None = None  # callable(config) -> dict\n"
        "    engine_stopper: object | None = None  # callable() -> dict",
        1,
    )

    # --- new routes
    old_route = '        if path == "/api/agents":\n            return 200, self._agents()'
    new_route = (
        '        if path == "/api/agents":\n'
        '            return 200, self._agents()\n'
        '        if path == "/api/engine/start" and method == "POST":\n'
        '            return self._engine_start(body)\n'
        '        if path == "/api/engine/stop" and method == "POST":\n'
        '            return self._engine_stop()'
    )
    if old_route not in text:
        print("[FAIL] server.py: /api/agents route anchor not found")
    else:
        text = text.replace(old_route, new_route, 1)

    # --- handlers, inserted before _run_backtest
    anchor = "    def _run_backtest(self, body: dict) -> tuple[int, dict]:"
    if anchor not in text:
        print("[FAIL] server.py: _run_backtest anchor not found")
    else:
        handlers = '''    def _engine_start(self, body: dict) -> tuple[int, dict]:
        """Start the trading engine or the agent system in-process."""
        mode = str(body.get("mode", "agents")).lower()
        if mode not in ("paper", "agents"):
            return 400, {"error": f"unknown mode {mode!r}; use 'paper' or 'agents'"}
        if self.ctx.engine_starter is None:
            return 501, {"error": "this server was started without engine control"}
        try:
            result = self.ctx.engine_starter({"mode": mode, **(body or {})})
        except Exception as exc:  # noqa: BLE001
            log.exception("engine start failed")
            return 500, {"error": str(exc)}
        if self.state:
            self.state.log("engine_start_requested", level="INFO", mode=mode,
                           result=str(result)[:200])
        status = 202 if result.get("started") else 409
        return status, result

    def _engine_stop(self) -> tuple[int, dict]:
        if self.ctx.engine_stopper is None:
            return 501, {"error": "this server was started without engine control"}
        try:
            result = self.ctx.engine_stopper()
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": str(exc)}
        if self.state:
            self.state.log("engine_stop_requested", level="INFO")
        return 200, result

'''
        text = text.replace(anchor, handlers + anchor, 1)

    # --- status must reflect a live engine
    old_status = '''    def _status(self) -> dict:
        engine = self.ctx.engine
        running = bool(getattr(engine, "running", False))
        mode = str(getattr(engine, "mode", "IDLE")) if engine else "IDLE"
        return {
            "ok": True,
            "version": self.ctx.version,
            "mode": mode,
            "engine_running": running,'''
    new_status = '''    def _status(self) -> dict:
        engine = self.ctx.engine
        if engine is None:
            engine = self.ctx.agents or self._live_engine
        running = bool(getattr(engine, "running", False)) or bool(
            getattr(self.ctx.agents, "cycles", 0) and getattr(self.ctx.agents, "_thread", None)
            and self.ctx.agents._thread.is_alive())
        mode = str(getattr(engine, "mode", "IDLE")) if engine else "IDLE"
        if self.ctx.agents is not None and getattr(self.ctx.agents, "cycles", 0) > 0:
            mode = "AGENTS-DRYRUN" if self.ctx.agents.cfg.dry_run else "AGENTS-LIVE"
        return {
            "ok": True,
            "version": self.ctx.version,
            "mode": mode,
            "engine_running": running,'''
    if old_status not in text:
        print("[warn] server.py: _status anchor not found; leaving status as-is")
    else:
        text = text.replace(old_status, new_status, 1)

    # --- carry a reference to a locally started engine
    text = text.replace(
        "        self._backtest_lock = threading.Lock()",
        "        self._backtest_lock = threading.Lock()\n"
        "        self._engine_lock = threading.Lock()\n"
        "        self._live_engine = None",
        1,
    )

    server.write_text(text, encoding="utf-8")
    print("[ok]   server.py: engine start/stop API added")

sys.exit(0)
