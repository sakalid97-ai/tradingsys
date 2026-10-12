"""Create the desktop shortcut and launcher for the dashboard.

Run once:  py tradingsys\\scripts\\make_shortcut.py

Produces:
  * ``tradingsys\\dashboard.ico``  -- generated 32px icon (no external deps)
  * ``tradingsys\\Dashboard.bat``  -- launcher
  * Desktop shortcut ``Trading Dashboard.lnk``
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = Path.home() / "Desktop"

# --------------------------------------------------------------- icon (pure stdlib)

WIDTH = HEIGHT = 32
BG = (11, 17, 24)
GREEN = (52, 211, 153)
CYAN = (56, 189, 248)


def _chart_points() -> list[tuple[int, int]]:
    """A rising candlestick-free trend line across the icon."""
    return [(5, 24), (12, 15), (19, 19), (27, 7)]


def _dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def build_icon() -> bytes:
    """Render a 32x32 BGRA bitmap in ICO container."""
    pixels = [[tuple(BG) + (255,) for _ in range(WIDTH)] for _ in range(HEIGHT)]
    pts = _chart_points()

    # trend line, 2px wide
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        for y in range(HEIGHT):
            for x in range(WIDTH):
                d = _dist_to_segment(x + 0.5, y + 0.5, ax, ay, bx, by)
                if d <= 1.4:
                    pixels[y][x] = GREEN + (255,)

    # nodes
    for i, (cx, cy) in enumerate(pts):
        for y in range(HEIGHT):
            for x in range(WIDTH):
                if ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 <= 2.2:
                    pixels[y][x] = (CYAN if i == len(pts) - 1 else (230, 245, 255)) + (255,)

    # area fill under the line
    polyline: list[float] = []
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        for y in range(HEIGHT):
            for x in range(WIDTH):
                d = _dist_to_segment(x + 0.5, y + 0.5, ax, ay, bx, by)
                polyline.append(d)

    raw = bytearray()
    for y in range(HEIGHT - 1, -1, -1):          # bottom-up rows
        for x in range(WIDTH):
            b, g, r, a = pixels[y][x]
            raw += bytes((b, g, r, 255))         # BMP has no alpha channel
    # AND mask: 32 rows x 4 bytes (1bpp padded), all zero = fully opaque
    raw += bytes(HEIGHT * 4)

    header = struct.pack("<H", 40)
    header += struct.pack("<llHHHHllll", WIDTH, HEIGHT * 2, 1, 32, 0, len(raw), 0, 0, 0, 0)

    image = bytes(raw)
    ico_dir = struct.pack("<HHH", 0, 1, 1)
    ico_entry = struct.pack("<BBBBHHII", WIDTH % 256, HEIGHT % 256, 0, 0, 1, 32,
                            len(header) + len(image), 22)
    return ico_dir + ico_entry + header + image


def write_png_for_reference() -> None:  # pragma: no cover - diagnostic only
    pass


# ------------------------------------------------------------------- launcher

def bat_contents(python_exe: str) -> str:
    return f'''@echo off
REM ---------------------------------------------------------------
REM  Autonomous Trading System -- dashboard launcher
REM  Removes itself from view when started from the desktop shortcut
REM ---------------------------------------------------------------
cd /d "{ROOT}"

if not exist "state" mkdir "state"

echo ============================================
echo   AUTONOMOUS TRADING SYSTEM
echo   dashboard starting...
echo ============================================

"{python_exe}" run.py dashboard --port 8642

echo.
echo Dashboard stopped. Press any key to close.
pause >nul
'''


def make_lnk_via_vbs(lnk_path: Path, target: Path, workdir: Path, icon: Path,
                     description: str) -> bool:
    """Create a true .lnk using Windows Script Host (no pywin32 needed)."""
    vbs = f'''Set sh = CreateObject("WScript.Shell")
Set lnk = sh.CreateShortcut("{lnk_path}")
lnk.TargetPath = "{target}"
lnk.WorkingDirectory = "{workdir}"
lnk.IconLocation = "{icon},0"
lnk.Description = "{description}"
lnk.WindowStyle = 7
lnk.Save()
'''
    temp = ROOT / "scripts" / "_mklnk.vbs"
    temp.write_text(vbs, encoding="utf-8")
    try:
        import subprocess

        result = subprocess.run(
            ["cscript", "//Nologo", "//E:vbscript", str(temp)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0 or not lnk_path.exists():
            print(f"[!] WScript failed: {result.stderr.strip()[:200]}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[!] could not run WScript: {exc}")
        return False
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def main() -> int:
    sys.path.insert(0, str(ROOT))

    # 1. icon
    icon_path = ROOT / "dashboard.ico"
    icon_path.write_bytes(build_icon())
    print(f"[OK] icon      -> {icon_path}")

    # 2. launcher
    python_exe = sys.executable
    bat_path = ROOT / "Dashboard.bat"
    bat_path.write_text(bat_contents(python_exe), encoding="utf-8")
    print(f"[OK] launcher  -> {bat_path}")

    # 3. desktop shortcut -- prefer a real .lnk, fall back to a .url
    DESKTOP.mkdir(parents=True, exist_ok=True)
    lnk_path = DESKTOP / "Trading Dashboard.lnk"
    description = "Autonomous Trading System dashboard"

    if make_lnk_via_vbs(lnk_path, bat_path, ROOT, icon_path, description):
        print(f"[OK] shortcut  -> {lnk_path}")
    else:
        url_path = DESKTOP / "Trading Dashboard.url"
        url_path.write_text(
            "[InternetShortcut]\n"
            f"URL=http://127.0.0.1:8642/\n"
            f"IconFile={icon_path}\n"
            "IconIndex=0\n",
            encoding="utf-8",
        )
        print(f"[OK] shortcut  -> {url_path}  (launch Dashboard.bat first)")

    # 4. start-menu entry too, since it costs nothing
    start_menu = (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs" / "Trading Dashboard.lnk"
    )
    if make_lnk_via_vbs(start_menu, bat_path, ROOT, icon_path, description):
        print(f"[OK] startmenu -> {start_menu}")

    print("\nDone. Double-click 'Trading Dashboard' on your desktop to start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
