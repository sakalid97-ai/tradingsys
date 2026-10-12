"""Entry point:  py run.py <command>

Keeps imports working whether launched from this folder or elsewhere.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ts.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
