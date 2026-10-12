"""Instrument the supervisor to find where the agent chain breaks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts.agents.feed import MarketFeed
from ts.agents.supervisor import MultiAgentSupervisor, SupervisorConfig

feed = MarketFeed(interval="1h", range_="60d")
symbols = ["SPX500", "BTCUSD", "ETHUSD", "SOLUSD", "EURUSD", "GBPUSD"]
sup = MultiAgentSupervisor(
    feed=feed, symbols=symbols,
    config=SupervisorConfig(cycle_seconds=1, interval="1h", range_="60d", dry_run=True),
)
sup.setup()

result = sup.cycle()
print("plans returned:", len(result["plans"]))
print("edge.plans      :", len(sup.edge.plans))
print("edge.analysis   :", list(sup.edge.analysis.keys()))
print("edge.probability:", list(sup.edge.probabilities.keys()))
print("analysis.latest :", list(sup.analysis.latest.keys()))
print("prob.latest     :", list(sup.probability.latest.keys()))
print()

for name, agent in [(a.name, a) for a in sup.agents]:
    print(f"{name:<14} alarms={agent.status.alarms[-3:]}")

print("\n--- analysis payloads ---")
for sym, payload in sup.analysis.latest.items():
    print(sym, {k: payload[k] for k in
                ("regime", "atr", "price", "stale", "trend_quality", "momentum")})

print("\n--- probability payloads ---")
for sym, payload in sup.probability.latest.items():
    print(sym, {k: payload[k] for k in ("p_up", "confidence", "concentration")
                if k in payload}, payload.get("note"))

print("\n--- edge plans detail ---")
for plan in sup.edge.plans:
    print(" ", plan.get("symbol"), plan.get("side"),
          "exp=", plan.get("expectancy_r"), "conf=", plan.get("confidence"),
          "qty=", plan.get("quantity"), "|", plan.get("reason"))
