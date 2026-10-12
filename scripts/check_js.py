"""Static sanity check for the dashboard front-end.

Two failure modes blank the entire dashboard rather than one panel, and both are
cheap to catch statically:

1. **An unbalanced backtick.** A stray `` ` `` inside a template literal is a
   syntax error for the whole file, so *nothing* renders and every header stat
   shows an em dash. This actually happened.
2. **A selector that matches no element.** ``$("#v-euity")`` silently selects
   nothing, the assignment lands on an empty set, and the panel stays blank with
   no error anywhere.

Runs in CI:  py tradingsys\\scripts\\check_js.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "ts" / "web" / "static"
JS = STATIC / "app.js"
HTML = STATIC / "index.html"

REQUIRED_FUNCTIONS = (
    "api", "refresh", "refreshAgents", "renderStatus", "renderOverview",
    "renderPositions", "renderTrades", "renderBacktest", "renderScanner",
    "renderLogs", "refreshBacktest", "refreshScanner",
)


def check_backticks(src: str) -> list[str]:
    """Total backtick parity is the reliable signal.

    A multi-line template literal legitimately contributes exactly one backtick
    to a given line, so a per-line check produces false positives. What matters
    is that the whole file has an even count -- an odd total means the file will
    not parse at all and every panel goes blank.
    """
    problems: list[str] = []
    total = src.count("`")
    if total % 2 != 0:
        problems.append(f"app.js: ODD total backtick count ({total}) -- the file will "
                        "not parse at all, and every panel will silently stay blank")
    return problems


def check_selectors(js: str, html: str) -> list[str]:
    """Every $("#id") / $$("#id") used in JS must exist in the HTML."""
    problems: list[str] = []
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\$\?\(["\']#([A-Za-z0-9_-]+)["\']\)', js))
    used |= set(re.findall(r'\$\(["\']#([A-Za-z0-9_-]+)["\']\)', js))
    for selector in sorted(used):
        if selector not in html_ids:
            problems.append(f'app.js references #{selector} but index.html declares no '
                            f'id="{selector}" -- that panel will silently stay blank')
    return problems


def check_functions(js: str) -> list[str]:
    return [f"app.js is missing function {fn}()" for fn in REQUIRED_FUNCTIONS
            if f"function {fn}" not in js]


def check_nav_views(html: str) -> list[str]:
    """Every .nav-item data-view must have a matching .view section."""
    problems: list[str] = []
    nav = set(re.findall(r'data-view="([^"]+)"', html))
    sections = set(re.findall(r'class="view[^"]*" data-view="([^"]+)"', html))
    for view in sorted(nav - sections):
        problems.append(f'nav item "{view}" has no matching <section class="view">')
    return problems


def main() -> int:
    if not JS.exists() or not HTML.exists():
        print("[FAIL] could not find dashboard static files")
        return 1

    js = JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    problems: list[str] = []
    problems += check_backticks(js)
    problems += check_functions(js)
    problems += check_selectors(js, html)
    problems += check_nav_views(html)

    if problems:
        print(f"[FAIL] dashboard front-end ({len(problems)} problem(s))")
        for problem in problems:
            print("   -", problem)
        return 1

    n_ids = len(re.findall(r'id="', html))
    print(f"[OK] dashboard front-end: {len(js.splitlines())} JS lines, "
          f"{n_ids} element ids, all selectors resolve, all views wired")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
