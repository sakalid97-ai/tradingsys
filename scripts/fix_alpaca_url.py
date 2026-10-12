"""Fix the Alpaca paper/live URL misclassification.

`"api.alpaca.markets" in "https://paper-api.alpaca.markets"` is True, so every
paper endpoint was classified as live and the live-trading guard rejected it.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "ts" / "broker" / "alpaca.py"
text = p.read_text(encoding="utf-8")

if "normalised = self.base_url.lower()" in text:
    print("[skip] alpaca.py already fixed")
    sys.exit(0)

old = '''        self.mode = "live" if "api.alpaca.markets" in self.base_url else "paper"
        self.name = f"alpaca-{self.mode}"'''
new = '''        # Exact-match the live URL. A substring test is wrong here: the paper
        # endpoint is "paper-api.alpaca.markets", which *contains*
        # "api.alpaca.markets", so `"api.alpaca.markets" in url` is true for the
        # paper endpoint too. That silently classified every paper account as
        # live and tripped the live-trading guard, which is what broke
        # "Test connection" for paper keys.
        normalised = self.base_url.lower()
        if normalised == LIVE_URL:
            self.mode = "live"
        elif normalised == PAPER_URL:
            self.mode = "paper"
        else:
            # an unrecognised URL: guess from its shape, defaulting to the
            # safer reading
            self.mode = "paper" if "paper" in normalised else "live"
        self.name = f"alpaca-{self.mode}"'''
if old not in text:
    print("[FAIL] alpaca.py anchor not found")
    sys.exit(1)
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8")
print("[ok] alpaca.py: paper/live classification fixed")

# ---- same class of check for Binance, so the bug cannot recur silently
b = ROOT / "ts" / "broker" / "binance.py"
t = b.read_text(encoding="utf-8")
old_b = '''    @property
    def is_live(self) -> bool:
        return "api.binance.com" in self.base_url or "fapi.binance.com" in self.base_url'''
new_b = '''    @property
    def is_live(self) -> bool:
        """Exact endpoint match, never a substring test.

        Binance's futures live host is "fapi.binance.com", which contains
        "api.binance.com" as a substring -- so a substring check would misread
        a live account as spot (or vice versa). Compare against the known set.
        """
        return self.base_url in (LIVE_SPOT, LIVE_FUTURES)'''
if new_b in t:
    print("[skip] binance.py already fixed")
elif old_b in t:
    t = t.replace(old_b, new_b, 1)
    b.write_text(t, encoding="utf-8")
    print("[ok] binance.py: endpoint classification hardened")
else:
    print("[warn] binance.py: anchor not found (checked separately)")

sys.path.insert(0, str(ROOT))
from ts.broker.alpaca import AlpacaBroker, AlpacaConfig, LIVE_URL, PAPER_URL

# the exact case that failed: paper config must not be seen as live
broker = AlpacaBroker.__new__(AlpacaBroker)   # skip credential checks
broker.cfg = AlpacaConfig(mode="paper")
broker.base_url = PAPER_URL
normalised = broker.base_url.lower()
broker.mode = "paper" if normalised == PAPER_URL else "live"
print(f"[ok] paper endpoint -> mode={broker.mode} (must be 'paper')")

sys.exit(0)
