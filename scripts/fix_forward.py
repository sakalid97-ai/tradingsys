"""Fix two real bugs found by the forward-journal tests."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "ts" / "forward.py"
text = p.read_text(encoding="utf-8")

# --- BUG 1: a malformed (or half-written) record made the whole journal
# unreadable. A crash mid-append leaves a truncated last line, which parsed as
# JSON but was missing required fields, raising TypeError and losing every
# trade in the file.
old_load = '''                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out.append(ForwardTrade(**{k: v for k, v in payload.items()
                                           if k in ForwardTrade.__dataclass_fields__}))'''
new_load = '''                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise TypeError("record is not an object")
                except json.JSONDecodeError:
                    continue
                try:
                    out.append(ForwardTrade(**{
                        k: v for k, v in payload.items()
                        if k in ForwardTrade.__dataclass_fields__}))
                except (TypeError, KeyError) as exc:
                    # a truncated or hand-edited record must not take the whole
                    # journal with it
                    log.warning("skipping unreadable forward record: %s", exc)
                    continue'''
if new_load in text:
    print("[skip] load() already robust")
elif old_load not in text:
    print("[FAIL] load() anchor not found")
    sys.exit(1)
else:
    text = text.replace(old_load, new_load, 1)
    print("[ok] load(): malformed records are skipped, not fatal")

# --- BUG 2: a sample with no variance collapses the CI to a point, so any
# prediction not identical to the sample mean is "outside" -- which makes
# identical trades look like decisive drift.
old_mean = '''    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    z = {0.90: 1.6449, 0.95: 1.95996, 0.99: 2.5758}.get(round(confidence, 2), 1.95996)
    margin = z * std / math.sqrt(n)
    return mean - margin, mean + margin'''
new_mean = '''    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    z = {0.90: 1.6449, 0.95: 1.95996, 0.99: 2.5758}.get(round(confidence, 2), 1.95996)
    margin = z * std / math.sqrt(n)

    # Floor the half-width so a zero-variance sample cannot produce a
    # degenerate interval. Without this, a run of identical R-multiples gives
    # an interval of zero width, and every prediction other than the exact
    # sample mean lands outside it -- reporting noise as decisive drift.
    scale = max(abs(mean), 0.5)
    margin = max(margin, 0.10 * math.sqrt(scale) + 0.05)
    return mean - margin, mean + margin'''
if new_mean in text:
    print("[skip] mean_interval already floored")
elif old_mean not in text:
    print("[FAIL] mean_interval anchor not found")
    sys.exit(1)
else:
    text = text.replace(old_mean, new_mean, 1)
    print("[ok] mean_interval(): half-width floored, no degenerate intervals")

p.write_text(text, encoding="utf-8")
sys.path.insert(0, str(ROOT))
from ts.forward import ForwardJournal, mean_interval
print("[ok] imports clean")
sys.exit(0)
