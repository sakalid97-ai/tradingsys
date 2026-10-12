"""Fix self.state -> self.ctx.state inside DashboardServer handlers only."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
server = ROOT / "ts" / "web" / "server.py"
lines = server.read_text(encoding="utf-8").splitlines(keepends=True)

# AppContext.set_killed legitimately uses self.state; that appears before the
# DashboardServer class, so only rewrite references after the class begins.
class_start = None
for i, line in enumerate(lines):
    if line.startswith("class DashboardServer"):
        class_start = i
        break
if class_start is None:
    print("[FAIL] DashboardServer class not found")
    sys.exit(1)

changed = 0
for i in range(class_start, len(lines)):
    if "self.state" in lines[i]:
        lines[i] = lines[i].replace("self.state", "self.ctx.state")
        changed += 1

server.write_text("".join(lines), encoding="utf-8")
print(f"[ok]   fixed {changed} reference(s) inside DashboardServer")

sys.path.insert(0, str(ROOT))
try:
    from ts.web.server import DashboardServer  # noqa: F401
    print("[ok]   server.py imports cleanly")
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] import error: {exc}")
sys.exit(0)
