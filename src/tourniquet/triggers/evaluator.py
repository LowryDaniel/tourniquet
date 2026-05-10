"""Trigger evaluator — scaffolded in W1, anomaly rule turns on in W4.

Trigger conditions (condition_json):
  {"type": "spend_threshold_pct", "pct": 80}    — % of daily cap
  {"type": "spend_3x_baseline"}                  — 3× rolling-7d average (W4 anomaly rule)

Trigger actions (actions_json):
  {"alert": true}                                — send email alert
  {"kill": true}                                 — enable kill switch
  {"alert": true, "kill": false}                 — alert only

All triggers with enabled=False are skipped silently.
The anomaly rule (spend_3x_baseline) will be enabled in W4 once real
baseline data exists. Turning it on cold causes false positives.
"""

from __future__ import annotations

from typing import Any


async def evaluate(
    *,
    condition: dict[str, Any],
    spent_usd_cents: int,
    cap_usd_cents: int,
    enabled: bool,
) -> bool:
    """Return True if the trigger condition is met.

    Does not fire actions — caller handles that.
    """
    if not enabled:
        return False

    ctype = condition.get("type")

    if ctype == "spend_threshold_pct":
        threshold_pct = condition.get("pct", 80)
        pct = (spent_usd_cents / cap_usd_cents * 100) if cap_usd_cents else 0
        return bool(pct >= threshold_pct)

    if ctype == "spend_3x_baseline":
        # Anomaly rule: off until W4 baseline data exists.
        # Implementation: compare today's spend to rolling-7d average.
        # Placeholder — returns False until enabled.
        return False

    return False
