"""Spending profiles — preset bundles of (kill default, alert thresholds).

Three profiles, three different chattiness levels. **All default to
`kill_enabled = True`** — protecting your budget is the default, always.
Disabling the kill switch is an explicit, deliberate action.

The `monitor` profile leaves kill_enabled OFF so alerts fire without
enforcing — but EVERY alert message contains a one-click "Kill now" link
so the user can re-enforce in seconds when they decide they need to.

Profiles affect ONLY:
  - default kill_enabled value when the key is created
  - which threshold percentages fire alerts
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    default_kill_enabled: bool
    alert_thresholds: list[int]  # percentages of cap that fire alerts
    description: str  # one-sentence pitch shown in the UI
    pick_me_if: str  # plain-English hint shown in dropdown help


PROFILES: dict[str, Profile] = {
    "standard": Profile(
        name="standard",
        default_kill_enabled=True,
        alert_thresholds=[50, 80, 100],
        description="Hard kill at 100% with warnings at 50% and 80%.",
        pick_me_if="you want a firm budget wall with lead time before it.",
    ),
    "strict": Profile(
        name="strict",
        default_kill_enabled=True,
        alert_thresholds=[100],
        description="Hard kill at 100%. One alert when blocked, no chatter.",
        pick_me_if="you want minimum noise and a sharp wall.",
    ),
    "monitor": Profile(
        name="monitor",
        default_kill_enabled=False,
        alert_thresholds=[50, 80, 100],
        description=(
            "Alerts only — does NOT auto-kill. Every alert includes a "
            "one-click kill link so you can enforce manually."
        ),
        pick_me_if=(
            "you run production traffic that mustn't break, but you want "
            "tight notifications you can act on."
        ),
    ),
}


def get_profile(name: str) -> Profile:
    return PROFILES.get(name, PROFILES["standard"])
