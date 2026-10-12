"""Fix BOM handling in universe files and make CLI output console-safe."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

# ------------------------------------------------------- 1. cli.py console safety
cli = ROOT / "ts" / "cli.py"
text = cli.read_text(encoding="utf-8")

if "reconfigure" in text:
    print("[skip] cli.py: stdout already reconfigured")
else:
    anchor = "def setup_logging(verbose: bool = False) -> None:"
    if anchor not in text:
        print("[FAIL] cli.py: setup_logging anchor not found")
    else:
        text = text.replace(
            anchor,
            '''def _make_console_safe() -> None:
    """Avoid a UnicodeEncodeError on a cp1252 Windows console.

    The CLI prints arrows and box-drawing characters; a default Windows console
    would crash with 'charmap codec can\\'t encode' the first time one appears.
    Reconfiguring stdout with errors="replace" turns that into a '?' instead.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):  # redirected/piped output
        pass


def setup_logging(verbose: bool = False) -> None:''',
            1,
        )
        # call it first thing in main()
        text = text.replace(
            "def main(argv: list[str] | None = None) -> int:\n    parser = build_parser()",
            "def main(argv: list[str] | None = None) -> int:\n"
            "    _make_console_safe()\n"
            "    parser = build_parser()",
            1,
        )
        cli.write_text(text, encoding="utf-8")
        print("[ok]   cli.py: stdout reconfigured to utf-8/replace")

# ------------------------------------------------------- 2. BOM in universe files
if 'read_text(encoding="utf-8")' in text:
    text = text.replace(
        'file = Path(path)\n    if not file.exists():\n        raise SystemExit(f"universe file not found: {path}")\n    symbols = []\n    for line in file.read_text(encoding="utf-8").splitlines():',
        'file = Path(path)\n'
        '    if not file.exists():\n'
        '        raise SystemExit(f"universe file not found: {path}")\n'
        '    symbols = []\n'
        '    # utf-8-sig strips a byte-order mark, which otherwise becomes part of'
        ' the\n'
        '        # first symbol and makes every lookup for it fail\n'
        '    for line in file.read_text(encoding="utf-8-sig").splitlines():',
        1,
    )
    cli.write_text(text, encoding="utf-8")
    print("[ok]   cli.py: universe reader strips a UTF-8 BOM")
else:
    print("[skip] cli.py: BOM fix anchor not found")

sys.exit(0)
