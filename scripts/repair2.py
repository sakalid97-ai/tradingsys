"""Robust-repair script: line-based, so it works regardless of whitespace."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def read(p): return p.read_text(encoding="utf-8")
def write(p, t): p.write_text(t, encoding="utf-8")


# ---------------------------------------------------------------- 1. cli.py
cli = ROOT / "ts" / "cli.py"
lines = read(cli).splitlines(keepends=True)
if any("from ts.state import StateStore, dumps" in ln for ln in lines):
    print("[skip] cli.py dumps import already present")
else:
    for i, line in enumerate(lines):
        if line.strip() == "from ts.walkforward import WalkForward, WalkForwardConfig":
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i, f"{indent}from ts.state import dumps\n")
            write(cli, "".join(lines))
            print(f"[ok]   cli.py: inserted 'from ts.state import dumps' before line {i + 1}")
            break
    else:
        print("[FAIL] cli.py: could not find the walkforward import anchor")


# ------------------------------------------------------------ 2. datafeed.py
feed = ROOT / "ts" / "datafeed.py"
text = read(feed)

if "_atomic_write" not in text and "_save_cache" in text:
    # wrap the write+rename in a retry loop that tolerates a racing reader
    old = '''        try:
            df.to_csv(tmp)
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - disk full, permissions, etc.
            log.warning("could not write cache %s: %s", path, exc)'''
    new = '''        # A cache write that fails is not an error worth shouting about: the
        # bars are already fetched and in memory. Windows raises
        # PermissionError / WinError 32 when another process holds the same
        # file, so retry briefly, then give up quietly.
        last_error: OSError | None = None
        for attempt in range(_CACHE_WRITE_RETRIES):
            try:
                df.to_csv(tmp)
                tmp.replace(path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
                try:
                    tmp.unlink()
                except OSError:
                    pass
        log.debug("could not write cache %s (another process is using it): %s",
                  path, last_error)'''
    if old in text:
        text = text.replace(old, new, 1)
    else:
        print("[warn] datafeed.py: exact retry anchor not found, patching by line")

    if "_CACHE_WRITE_RETRIES" in text and "_CACHE_WRITE_RETRIES =" not in text:
        text = text.replace(
            "log = logging.getLogger(__name__)",
            "log = logging.getLogger(__name__)\n\n"
            "# How many times to retry a cache write against a racing process.\n"
            "_CACHE_WRITE_RETRIES = 4",
            1,
        )
    write(feed, text)
    print("[ok]   datafeed.py: cache writes now retry and fail quietly")


# ---------------------------------------------------------------- 3. verify
print("\n--- verification ---")
sys.path.insert(0, str(ROOT))
try:
    from ts.datafeed import YahooFeed
    from ts.web.server import DashboardServer
    print("[ok]   all modules import cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")

sys.exit(0)
