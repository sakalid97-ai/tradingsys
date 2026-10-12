"""Document the analyst adapter and fix the demo's odds-multiplier line."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- demo_analyst
demo = ROOT / "scripts" / "demo_analyst.py"
text = demo.read_text(encoding="utf-8")
bad = "{2 ** 0.28:.2f}x-ish)"
good = "{2.718281828 ** (2 * adapter.cfg.max_log_odds * adapter.cfg.skill_scale):.2f}x)"
if good in text:
    print("[skip] demo_analyst.py: odds line already correct")
elif bad in text:
    text = text.replace(bad, good, 1)
    demo.write_text(text, encoding="utf-8")
    print("[ok]   demo_analyst.py: odds multiplier now accurate")
else:
    print("[warn] demo_analyst.py: odds line anchor not found")

# -------------------------------------------------------------------- README
readme = ROOT / "README.md"
text = readme.read_text(encoding="utf-8")

if "## Plugging in an LLM analyst" in text:
    print("[skip] README.md: analyst section already present")
else:
    section = '''## Plugging in an LLM analyst (FinRobot / CrewAI)

`ts/agents/analyst.py` bridges a language model into the Bayesian engine **without
letting it take over the risk path**. Try it:

```
py tradingsys\\scripts\\demo_analyst.py --simulate-skill good
py tradingsys\\scripts\\demo_analyst.py --simulate-skill none   # untested -> zero influence
py tradingsys\\scripts\\demo_analyst.py --simulate-skill bad    # invert
```

| Analyst track record | BF on a bullish call | Effect on the posterior |
|---|---|---|
| untested (< 10 scored calls) | **1.0000** | does not move at all |
| 50% (a coin flip) | 1.0000 | no influence |
| 80% accurate | 1.497 | leans it upward |
| 20% accurate | **0.668** | **inverts** -- argues for a fall |

Three rules make this safe:

1. **An unproven analyst has zero influence.** Until at least
   ``min_observations`` calls have been scored, ``skill()`` is 0, so the
   log-odds contribution is exactly 0 and the Bayes factor is exactly 1.0.
   Calibration has to be *earned* from a track record, not assumed from a
   prompt.
2. **Influence is bounded.** One call shifts the log-odds by at most
   ``max_log_odds x skill x skill_scale`` nats -- comparable to a single
   price-based feature. It is one opinion among several, never a veto, and even
   a perfect analyst cannot overrule strong price evidence.
3. **A bad analyst inverts.** Negative skill turns a bullish call into bearish
   evidence, which is the correct Bayesian treatment of a poorly-skilled source
   and falls out for free from calibrating on the track record.

Wiring your own model in takes one function -- no dependency on any framework:

```python
from ts.agents.analyst import AnalystAdapter

def call_finrobot(prompt: str) -> str:
    return finrobot.agents.analyse(prompt)      # or a CrewAI crew, or OpenAI

adapter = AnalystAdapter()
evidence = adapter.analyse("AAPL", context, call_finrobot)
if evidence is not None:
    engine.update(evidence)      # your existing BayesianEngine, unchanged
```

The live agent layer accepts it directly, so the multi-agent system uses it
without any other change:

```python
ProbabilityAgent(bus, analyst_adapter=adapter,
                 analyst_caller=call_finrobot,
                 context_provider=lambda symbol, analysis: build_context(symbol, analysis))
```

Every signal is journalled with direction, conviction, measured skill, log-odds
contribution, reasoning, model name and the current calibration table, so the
dashboard can show exactly what the model said and how much it was allowed to
matter.

**Rejection is the default.** Anything failing validation -- the wrong symbol, an
unknown direction, an out-of-range conviction, non-JSON prose -- returns `None`
rather than a fabricated signal. ``snapshot()["rejected"]`` reports the failure
count and the reason for each.

## What makes this not a fantasy'''

    if "## What makes this not a fantasy" not in text:
        print("[FAIL] README.md: anchor section missing")
    else:
        text = text.replace("## What makes this not a fantasy", section, 1)
        readme.write_text(text, encoding="utf-8")
        print("[ok]   README.md: analyst section inserted")

sys.exit(0)
