"""Add an SSE endpoint so the dashboard pushes rather than polls.

The engine already writes a snapshot to disk; this tails it and streams it, so a
1-second UI refresh costs one request instead of ten. The snapshot must carry
``quotes`` for the header ticker.
"""

from __future__ import annotations

import sys
from threading import Thread
from time import sleep
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ok = True


def patch(path: Path, subs, label: str) -> bool:
    global ok
    text = path.read_text(encoding="utf-8")
    applied = skipped = failed = 0
    for old, new in subs:
        if new and new in text:
            skipped += 1
            continue
        if old not in text:
            print(f"    [FAIL] {path.name}: {old[:56]!r}")
            failed += 1
            continue
        text = text.replace(old, new, 1)
        applied += 1
    if not failed:
        path.write_text(text, encoding="utf-8")
    print(f"[{'ok' if not failed else 'FAIL'}] {label}: "
          f"{applied} applied, {skipped} present, {failed} failed")
    ok = ok and not failed
    return not failed


# ---- the SSE route
patch(ROOT / "ts" / "web" / "server.py", [
    (
        '        if path == "/api/data-sources":\n            return 200, {"sources": _data_sources()}',
        '        if path == "/api/data-sources":\n            return 200, {"sources": _data_sources()}\n'
        '        if path == "/api/stream":\n'
        '            return 200, self._stream()',
    ),
], "server.py: /api/stream route")

patch(ROOT / "ts" / "web" / "server.py", [
    (
        "    # ---------------------------------------------------------------- forward",
        '''    def _stream(self):
        """Server-Sent Events: push the snapshot whenever it changes.

        Tailing the state file means the UI updates on the engine's cadence, not
        the browser's poll interval. One request, many updates.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_mtime = None
        last_payload = ""
        while True:
            try:
                path = self.ctx.state.root / "snapshot.json"
                mtime = path.stat().st_mtime if path.exists() else None
                if mtime and mtime != last_mtime:
                    last_mtime = mtime
                    payload = dumps({"type": "snapshot",
                                     "data": self.ctx.state.read_snapshot()})
                    if payload != last_payload:
                        last_payload = payload
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                sleep(1.0)
            except Exception:  # noqa: BLE001 - client disconnected
                break

    # ---------------------------------------------------------------- forward''',
    ),
], "server.py: SSE handler")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer

    print("[ok] imports clean")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0 if ok else 1)
