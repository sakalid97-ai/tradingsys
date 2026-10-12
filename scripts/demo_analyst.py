"""Demonstrate the LLM analyst adapter feeding the Bayesian engine.

    py tradingsys\\scripts\\demo_analyst.py --simulate-skill good
    py tradingsys\\scripts\\demo_analyst.py --simulate-skill none   # zero influence
    py tradingsys\\scripts\\demo_analyst.py --simulate-skill bad     # inverts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts.agents.analyst import (  # noqa: E402
    AnalystAdapter,
    AnalystAdapterConfig,
    StubAnalyst,
    build_prompt,
)
from ts.agents.bayesian import BayesianEngine, Evidence  # noqa: E402
from ts.agents.probability import DEFAULT_RULES  # noqa: E402

SYMBOL = "AAPL"

CONTEXT = """AAPL closed at 226.14, up 0.4% on the session.
Regime: above the 50-day moving average, 20-day above 50-day.
20-day breakout: yes.  ADX 27, R-squared 0.61, efficiency ratio 0.34.
Relative volume 1.3x.  No scheduled earnings within 5 sessions."""


def price_evidence(regime: int = 1, momentum: float = 0.30,
                   trend_quality: float = 0.61, efficiency: float = 0.34) -> list[Evidence]:
    """The same evidence the live analysis agent would produce for this state."""
    sign = 1.0 if regime >= 0 else -1.0
    out: list[Evidence] = []
    for rule in DEFAULT_RULES:
        feature = {
            "momentum": momentum * sign,
            "trend_quality": trend_quality * sign,
            "efficiency": efficiency * sign,
            "breakout": 1.0 if regime > 0 else -1.0,
        }.get(rule.source, 0.0)
        p_up, p_down = rule.probability(feature)
        out.append(Evidence(source=rule.source, p_if_up=p_up, p_if_down=p_down,
                            detail={"feature": round(feature, 3),
                                    "assumption": rule.description}))
    return out


def train(adapter: AnalystAdapter, accuracy: float | None) -> None:
    """Give the analyst a synthetic track record."""
    if accuracy is None:
        return
    total = max(adapter.cfg.min_observations + 5, 15)
    correct = int(round(accuracy * total))
    for index in range(total):
        adapter.record_outcome("llm_analyst", was_correct=(index < correct))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate-skill", default="good",
                        choices=["good", "coinflip", "bad", "none"],
                        help="synthetic analyst track record (default: good)")
    parser.add_argument("--max-log-odds", type=float, default=0.6)
    parser.add_argument("--min-observations", type=int, default=10)
    parser.add_argument("--skill-scale", type=float, default=0.7)
    parser.add_argument("--direction", default="bullish",
                        choices=["bullish", "bearish", "neutral"])
    args = parser.parse_args()

    adapter = AnalystAdapter(AnalystAdapterConfig(
        max_log_odds=args.max_log_odds,
        min_observations=args.min_observations,
        skill_scale=args.skill_scale,
    ))
    engine = BayesianEngine(SYMBOL, alpha0=2.0, beta0=2.0)

    train(adapter, {"good": 0.8, "coinflip": 0.5, "bad": 0.2,
                    "none": None}[args.simulate_skill])

    calibration = adapter.calibration_for("llm_analyst")
    print("=" * 68)
    print("LLM ANALYST ADAPTER -> BAYESIAN ENGINE")
    print("=" * 68)
    print(f"  analyst track record : {calibration.correct}/{calibration.total} correct"
          f"   skill={calibration.skill():+.3f}")
    print(f"  max log-odds / call  : {adapter.cfg.max_log_odds:+.2f} nats "
          f"(a single opinion can shift odds by at most "
          f"{2.718281828 ** (2 * adapter.cfg.max_log_odds * adapter.cfg.skill_scale):.2f}x)")
    if calibration.total < adapter.cfg.min_observations:
        print("  STATUS               : UNTESTED -- Bayes factor is exactly 1.0, "
              "zero influence")
    print()

    print("--- prompt sent to the analyst " + "-" * 40)
    print(build_prompt(SYMBOL, CONTEXT))
    print("-" * 68)

    analyst = StubAnalyst(responses={
        SYMBOL: json.dumps({
            "symbol": SYMBOL, "direction": args.direction,
            "conviction": 0.8, "horizon_days": 5,
            "reasoning": "20-day breakout with rising relative volume and a "
                         "confirmed trend regime; no earnings risk within the "
                         "holding window.",
        })})

    price = price_evidence()
    print(f"\n--- price-based evidence ({len(price)} sources) " + "-" * 29)
    for item in price:
        print(f"  {item.describe():<50}  BF={item.bayes_factor:.3f}")
    for item in price:
        engine.update(item)

    before = engine.posterior_mean
    before_conf = engine.confidence()

    llm = adapter.analyse(SYMBOL, CONTEXT, analyst)
    print(f"\n--- analyst call " + "-" * 47)
    if llm is None:
        print("  [rejected] response failed validation; engine unchanged")
        return 0
    print(f"  direction    : {llm.detail['direction']}")
    print(f"  conviction   : {llm.detail['conviction']}")
    print(f"  skill applied: {llm.detail['skill']:+.4f}")
    print(f"  log-odds     : {llm.detail['log_odds']:+.4f} nats")
    print(f"  Bayes factor : {llm.bayes_factor:.4f}")
    print(f"  reasoning    : {llm.detail['reasoning']}")

    engine.update(llm)
    after = engine.posterior_mean

    print("\n--- posterior " + "-" * 52)
    print(f"  P(up)        : {before:.4f}  ->  {after:.4f}   ({after - before:+.4f})")
    print(f"  confidence   : {before_conf:.4f}  ->  {engine.confidence():.4f}")
    lo, hi = engine.credible_interval()
    print(f"  90% CI       : [{lo:.4f}, {hi:.4f}]")
    print(f"  updates      : {engine.steps}")

    shift = after - before
    if abs(shift) < 1e-9:
        print("\n  >> ZERO influence: the analyst is untested, average, or neutral.")
    else:
        direction_word = "raised" if shift > 0 else "lowered"
        print(f"\n  >> the analyst {direction_word} the posterior by "
              f"{abs(shift):.4f}, bounded by design.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
