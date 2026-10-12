"""Trace the multi-agent pipeline and show intermediate values."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts.agents.feed import MarketFeed
from ts.agents.analysis import MarketAnalyzer
from ts.agents.bayesian import BayesianEngine
from ts.agents.kelly import KellyConfig, KellySizer, breakeven_probability
from ts.agents.probability import DEFAULT_RULES, EvidenceRule, ProbabilityAgent

feed = MarketFeed(interval="1h", range_="60d")
symbols = ["SPX500", "NAS100", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD", "SOLUSD"]
analyzer = MarketAnalyzer()

import ts.agents.probability as prob_mod


def _evidence_from_analysis(payload: dict):
    """Standalone copy of the agent's evidence mapping (avoids agent state)."""
    from ts.agents.bayesian import Evidence

    regime = int(payload.get("regime", 0) or 0)
    momentum = float(payload.get("momentum", 0.0) or 0.0)
    trend_quality = float(payload.get("trend_quality", 0.0) or 0.0)
    efficiency = float(payload.get("efficiency", 0.0) or 0.0)
    breakout_up = bool(payload.get("breakout_up", False))
    breakout_down = bool(payload.get("breakout_down", False))
    sign = 1.0 if regime >= 0 else -1.0
    breakout_signal = 1.0 if (breakout_up and regime >= 0) else (
        -1.0 if (breakout_down and regime < 0) else 0.0)
    out = []
    for rule in DEFAULT_RULES:
        feature = {
            "momentum": momentum * sign,
            "trend_quality": trend_quality * sign,
            "efficiency": efficiency * sign,
            "breakout": breakout_signal,
        }.get(rule.source, 0.0)
        p_up, p_down = rule.probability(feature)
        out.append(Evidence(source=rule.source, p_if_up=p_up, p_if_down=p_down,
                            detail={"feature": round(feature, 3)}))
    return out

print(f"{'symbol':<9}{'stale':>7}{'regime':>7}{'adxS':>7}{'atr%':>8}"
      f"{'tq':>6}{'mom':>7}{'eff':>6}{'p_up':>8}{'conf':>7}{'ev':>7}{'grade':>7}")
for sym in symbols:
    try:
        snap = feed.fetch(sym)
    except Exception as exc:
        print(f"{sym:<9} ERROR {exc}")
        continue
    a = analyzer.analyse(snap)
    payload = a.to_dict()
    engine = BayesianEngine(sym, alpha0=2.0, beta0=2.0, decay=0.995)
    evidence = _evidence_from_analysis(payload)
    engine.update_many(evidence)
    p_up = engine.posterior_mean
    conf = engine.confidence()

    regime = a.regime
    side = "long" if regime > 0 else "short" if regime < 0 else "none"
    payoff = 2.0
    if regime != 0:
        p_win = p_up if side == "long" else 1 - p_up
        ev = p_win * payoff - (1 - p_win)
        be = breakeven_probability(payoff)
    else:
        p_win, ev, be = 0.0, 0.0, 0.0

    print(f"{sym:<9}{str(a.stale):>7}{regime:>7}{a.regime_strength:>7.2f}"
          f"{a.volatility*100:>7.3f}%{a.trend_quality:>6.2f}{a.momentum:>7.2f}"
          f"{a.efficiency:>6.2f}{p_up:>8.3f}{conf:>7.2f}{ev:>7.2f}"
          f"{'A' if ev > 0.5 else 'B' if ev > 0.35 else 'C' if ev > 0.2 else 'D' if ev > 0 else 'F':>7}")

print("\nBayes factors applied on the last symbol:")
for ev in evidence:
    print(f"  {ev.describe():<50} feature={ev.detail.get('feature')}")
