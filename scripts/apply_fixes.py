"""Apply pending fixes that the edit tool could not apply reliably."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def patch(path: Path, old: str, new: str, label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        print(f"[skip] {label}: pattern not found")
        return False
    if new in text:
        print(f"[skip] {label}: already applied")
        return True
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[ok]   {label}")
    return True


ok = True

# ---- 1. cli.py: datetime/UTC are used but never imported
cli = ROOT / "ts" / "cli.py"
text = cli.read_text(encoding="utf-8")
if "from datetime import UTC, datetime" not in text:
    text = text.replace(
        "import argparse\nimport logging\nimport sys\nimport time\nfrom pathlib import Path",
        "import argparse\nimport logging\nimport sys\nimport time\nfrom datetime import UTC, datetime\nfrom pathlib import Path",
        1,
    )
    cli.write_text(text, encoding="utf-8")
    print("[ok]   cli.py datetime import")
else:
    print("[skip] cli.py datetime import: already present")

# ---- 2. datafeed.py: imports for a process/thread-unique cache temp name
feed = ROOT / "ts" / "datafeed.py"
text = feed.read_text(encoding="utf-8")
if "\nimport os\n" not in text:
    text = text.replace(
        "import logging\nimport random\nimport time",
        "import logging\nimport os\nimport random\nimport threading\nimport time",
        1,
    )
if "import os" in text and "import threading" in text:
    feed.write_text(text, encoding="utf-8")
    print("[ok]   datafeed.py imports (os, threading)")
else:
    print("[skip] datafeed.py imports: already present")

# ---- 3. datafeed.py: never let two writers share one .tmp file
feed = ROOT / "ts" / "datafeed.py"
text = feed.read_text(encoding="utf-8")
old_tmp = '        tmp = path.with_suffix(".tmp")'
new_tmp = (
    '        # A unique temp name per process AND thread: the dashboard and a\n'
    '        # backtest can both fetch the same symbol at the same time, and a\n'
    '        # shared ".tmp" makes one of them fail with Permission denied.\n'
    '        unique = f"{os.getpid()}-{threading.get_ident()}"\n'
    '        tmp = path.with_name(f"{path.stem}.{unique}.tmp")'
)
if new_tmp in text:
    print("[skip] datafeed.py unique temp name: already applied")
elif old_tmp in text:
    text = text.replace(old_tmp, new_tmp, 1)
    # stale temp files must not accumulate
    if "_TEMP_SUFFIX_GUARD" not in text:
        text = text.replace(
            new_tmp,
            new_tmp.replace("        unique =", "        # _TEMP_SUFFIX_GUARD\n        unique ="),
            1,
        )
    feed.write_text(text, encoding="utf-8")
    print("[ok]   datafeed.py unique cache temp name")
else:
    print("[FAIL] datafeed.py unique temp name: anchor not found")
    ok = False

# ---- 4. web/server.py: tolerate a browser closing the connection
server = ROOT / "ts" / "web" / "server.py"
text = server.read_text(encoding="utf-8")
if "_CLIENT_GONE" not in text:
    text = text.replace(
        'STATIC_DIR = Path(__file__).resolve().parent / "static"',
        'STATIC_DIR = Path(__file__).resolve().parent / "static"\n\n'
        '# A browser closing a tab or navigating away mid-request is routine for a\n'
        '# long-polling dashboard; catching it keeps the server quiet.\n'
        '_CLIENT_GONE = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)',
        1,
    )
    text = text.replace(
        "                except Exception as exc:  # noqa: BLE001 - never let the UI down\n"
        "                    log.exception(\"GET %s failed\", self.path)\n"
        "                    self._send_json(500, {\"error\": str(exc)})",
        "                except _CLIENT_GONE:\n"
        "                    self.close_connection = True\n"
        "                except Exception as exc:  # noqa: BLE001 - never let the UI down\n"
        "                    log.debug(\"GET %s failed: %s\", self.path, exc)\n"
        "                    try:\n"
        "                        self._send_json(500, {\"error\": str(exc)})\n"
        "                    except _CLIENT_GONE:\n"
        "                        self.close_connection = True",
        1,
    )
    text = text.replace(
        "                except Exception as exc:  # noqa: BLE001\n"
        "                    log.exception(\"POST %s failed\", self.path)\n"
        "                    self._send_json(500, {\"error\": str(exc)})",
        "                except _CLIENT_GONE:\n"
        "                    self.close_connection = True\n"
        "                except Exception as exc:  # noqa: BLE001\n"
        "                    log.debug(\"POST %s failed: %s\", self.path, exc)\n"
        "                    try:\n"
        "                        self._send_json(500, {\"error\": str(exc)})\n"
        "                    except _CLIENT_GONE:\n"
        "                        self.close_connection = True",
        1,
    )
    # readline() itself raises before do_GET is entered
    text = text.replace(
        "            def do_GET(self) -> None:  # noqa: N802",
        "            def handle_one_request(self) -> None:\n"
        "                # Swallow a client that vanished mid-request.\n"
        "                try:\n"
        "                    super().handle_one_request()\n"
        "                except _CLIENT_GONE:\n"
        "                    self.close_connection = True\n\n"
        "            def do_GET(self) -> None:  # noqa: N802",
        1,
    )
    server.write_text(text, encoding="utf-8")
    print("[ok]   web/server.py client-gone handling")
else:
    print("[skip] web/server.py client-gone handling: already applied")

print("\nverifying imports and compilation...")
sys.exit(0 if ok else 1)
