"""Run any CLI command from the dashboard.

Every engine action is a subprocess of ``py run.py <command>``. Running it
detached rather than in the request thread matters: ``walkforward`` takes about
ten minutes and would otherwise hold the HTTP connection open until the browser
gave up.

Output is appended to ``state/console.log`` and polled by the UI, so a long
command shows live progress rather than a spinner with no information.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

# Every command the CLI exposes, with the arguments worth surfacing.
# A command absent from this catalogue cannot be started from the UI, which is
# deliberate for `live`.
COMMANDS: dict[str, dict] = {
    "backtest": {
        "label": "Backtest",
        "description": "Run the strategy over history and save the result.",
        "args": {
            "range": {"type": "text", "default": "7d",
                      "help": "history range: 7d, 1mo, 1y, 5y, max"},
            "interval": {"type": "choice", "default": "1m",
                         "options": ["1m", "5m", "15m", "1h", "1d"],
                         "help": "bar interval"},
            "capital": {"type": "number", "default": 100000},
            "risk": {"type": "number", "default": 0.01, "help": "fraction risked per trade"},
            "adx": {"type": "number", "default": 20},
            "stop": {"type": "number", "default": 2.0},
            "trail": {"type": "number", "default": 3.0},
            "shorts": {"type": "flag", "help": "allow short entries"},
            "reference": {"type": "flag", "help": "also fit on the full sample, to show "
                                                  "how optimistic that is"},
        },
    },
    "walkforward": {
        "label": "Walk-forward validation",
        "description": "Optimise in-sample, then trade the out-of-sample window. "
                       "The only honest test of a parameterised strategy.",
        "warning": "Needs >= 60 days of daily bars; 1m data is refused because "
                   "7 days cannot validate anything.",
        "args": {
            "interval": {"type": "choice", "default": "1d",
                         "options": ["1d", "1h", "15m"], "help": "bar interval (1d for validation)"},
            "range": {"type": "text", "default": "5y"},
            "folds": {"type": "number", "default": 5},
            "capital": {"type": "number", "default": 100000},
            "objective": {"type": "choice", "default": "sharpe",
                          "options": ["sharpe", "expectancy", "cagr"]},
            "reference": {"type": "flag"},
        },
    },
    "agents": {
        "label": "Multi-agent sweep",
        "description": "One pass of the six agents over indices, forex and crypto.",
        "args": {
            "cycles": {"type": "number", "default": 1, "help": "0 runs forever"},
            "interval": {"type": "choice", "default": "1m",
                         "options": ["1m", "5m", "15m", "1h", "1d"]},
            "range": {"type": "text", "default": "7d"},
            "capital": {"type": "number", "default": 100000},
            "min-expectancy": {"type": "number", "default": 0.20},
            "min-confidence": {"type": "number", "default": 0.65},
            "kelly-fraction": {"type": "number", "default": 0.25},
            "risk": {"type": "number", "default": 0.01},
            "stop": {"type": "number", "default": 2.0},
            "target": {"type": "number", "default": 4.0},
        },
    },
    "paper": {
        "label": "Paper trading",
        "description": "Simulated fills against live prices. No orders leave the machine.",
        "args": {
            "interval": {"type": "choice", "default": "1m",
                         "options": ["1m", "5m", "15m", "1h", "1d"]},
            "capital": {"type": "number", "default": 100000},
            "ticks": {"type": "number", "default": 10, "help": "0 = run forever"},
            "interval-seconds": {"type": "number", "default": 60},
            "risk": {"type": "number", "default": 0.01},
            "max-positions": {"type": "number", "default": 8},
        },
    },
    "report": {
        "label": "Report",
        "description": "Print the metrics saved by the last backtest.",
        "args": {},
    },
    "cache": {
        "label": "Cache",
        "description": "Inspect or clear the local bar cache.",
        "requires_subcommand": True,
        "args": {
            "cache_command": {"type": "choice", "default": "list",
                              "options": ["list", "clear"], "help": "list or clear the cache"},
            "limit": {"type": "number", "default": 50, "help": "rows when listing"},
        },
    },
    "probe": {
        "label": "Data connectivity probe",
        "description": "Check the market-data source is reachable.",
        "args": {},
    },
    "check-js": {
        "label": "Dashboard check",
        "description": "Static checks on the dashboard front-end.",
        "internal": True,
        "args": {},
    },
    "tests": {
        "label": "Run the test suite",
        "description": "Every unit test in the system.",
        "internal": True,
        "args": {
            "suite": {"type": "choice", "default": "all",
                      "options": ["all", "test_suite", "test_agents", "test_analyst",
                                  "test_propfirm"]},
        },
    },
}


@dataclass
class CommandRun:
    """One command execution and everything we know about it."""

    command: str
    args: dict
    pid: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def to_dict(self, tail: int = 200) -> dict:
        return {
            "command": self.command,
            "label": COMMANDS.get(self.command, {}).get("label", self.command),
            "args": self.args,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "running": self.running,
            "output": self.lines[-tail:],
            "lines": len(self.lines),
        }


class ConsoleRunner:
    """Executes CLI commands in background subprocesses."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path or (ROOT.parent / "console.log")
        self._lock = threading.Lock()
        self._current: CommandRun | None = None
        self._history: list[CommandRun] = []
        self._process: subprocess.Popen | None = None

    # ------------------------------------------------------------------- lists

    def catalogue(self) -> list[dict]:
        out = []
        for name, spec in COMMANDS.items():
            if spec.get("internal"):
                continue
            out.append({
                "command": name,
                "label": spec["label"],
                "description": spec["description"],
                "warning": spec.get("warning"),
                "requires_subcommand": spec.get("requires_subcommand", False),
                "args": [
                    {"name": key, **value} for key, value in spec["args"].items()
                ],
            })
        return out

    # ------------------------------------------------------------------ running

    def _build_argv(self, command: str, args: dict) -> list[str]:
        if command == "probe":
            return [sys.executable, str(ROOT / "scripts" / "probe.py")]
        if command == "check-js":
            return [sys.executable, str(ROOT / "scripts" / "check_js.py")]
        if command == "tests":
            argv = [sys.executable, "-m", "unittest", "discover",
                    "-s", str(ROOT / "tests"), "-v"]
            suite = args.get("suite", "all")
            if suite != "all":
                argv = [sys.executable, str(ROOT / "tests" / f"{suite}.py")]
            return argv

        argv = [sys.executable, str(ROOT / "run.py"), command]
        if command == "cache":
            argv.append(str(args.get("cache_command", "list")))
            if str(args.get("cache_command")) == "list":
                argv += ["--limit", str(int(float(args.get("limit", 50))))]
        else:
            for key, value in (args or {}).items():
                if value is None or key == "cache_command":
                    continue
                flag = key.replace("_", "-")
                spec = COMMANDS.get(command, {}).get("args", {}).get(key, {})
                if spec.get("type") == "flag":
                    if value in (True, "true", "on", 1, "1"):
                        argv.append(f"--{flag}")
                elif value not in ("", False, None):
                    argv += [f"--{flag}", str(value)]
        return argv

    def start(self, command: str, args: dict | None = None) -> tuple[int, CommandRun]:
        with self._lock:
            if self._current is not None and self._current.running:
                return 409, self._current
            run = CommandRun(command=command, args=dict(args or {}),
                             started_at=datetime.now(UTC).isoformat())
            self._current = run
            self._history.append(run)
            if len(self._history) > 25:
                self._history = self._history[-25:]
        self._spawn(run)
        return 202, run

    def _spawn(self, run: CommandRun) -> None:
        argv = self._build_argv(run.command, run.args)
        try:
            handle = self.log_path.open("a", encoding="utf-8")
            handle.write(f"\n$ {' '.join(argv)}\n")
            handle.flush()
            process = subprocess.Popen(
                argv, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except (OSError, ValueError) as exc:
            run.finished_at = datetime.now(UTC).isoformat()
            run.exit_code = -1
            run.lines.append(f"could not start: {exc}")
            log.error("command %s failed to start: %s", run.command, exc)
            return

        with self._lock:
            run.pid = process.pid
            self._process = process

        threading.Thread(target=self._wait, args=(run, process), daemon=True).start()

    def _wait(self, run: CommandRun, process: subprocess.Popen) -> None:
        code = process.wait()
        run.finished_at = datetime.now(UTC).isoformat()
        run.exit_code = code
        run.lines.append(f"[exit {code}] {' '.join(self._build_argv(run.command, run.args))}")

    # ------------------------------------------------------------------ reading

    def status(self, tail: int = 200) -> dict:
        with self._lock:
            current = self._current.to_dict(tail=tail) if self._current else None
            return {
                "current": current,
                "running": bool(current and current["running"]),
                "history": [r.to_dict(tail=5) for r in reversed(self._history[-8:])],
                "log_path": str(self.log_path),
            }

    def tail_log(self, lines: int = 200) -> list[str]:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
            return content.splitlines()[-lines:]
        except OSError:
            return []

    def stop_current(self) -> dict:
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()
            return {"stopped": True, "pid": process.pid}
        return {"stopped": False, "reason": "nothing is running"}


__all__ = ["ConsoleRunner", "CommandRun", "COMMANDS"]
