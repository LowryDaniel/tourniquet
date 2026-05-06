"""Pre-built spending profiles.

Each profile defines alert thresholds and kill behaviour.
v2 will expose a custom rules editor; v1 uses these three only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    alert_thresholds: list[int]  # percentages of cap
    kill_at_pct: int | None      # None = never hard-kill
    kill_silently: bool          # False = pause-and-ask instead of hard kill


PROFILES: dict[str, Profile] = {
    "hobby": Profile(
        name="hobby",
        alert_thresholds=[80],
        kill_at_pct=200,
        kill_silently=True,
    ),
    "production": Profile(
        name="production",
        alert_thresholds=[50, 80, 100],
        kill_at_pct=100,
        kill_silently=True,
        # Note: kill_enabled on the api_key row defaults to FALSE for production profile.
        # User must explicitly opt in to hard kill on production keys.
    ),
    "demo": Profile(
        name="demo",
        alert_thresholds=[80],
        kill_at_pct=None,
        kill_silently=False,  # pause-and-ask, never silent kill
    ),
}


def get_profile(name: str) -> Profile:
    return PROFILES.get(name, PROFILES["hobby"])
