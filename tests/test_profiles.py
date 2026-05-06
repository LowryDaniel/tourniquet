"""Profile definitions — invariants that must hold across all profiles."""

import pytest

from burnrate.billing.profiles import PROFILES, get_profile


def test_three_profiles_exist():
    assert set(PROFILES.keys()) == {"hobby", "production", "demo"}


def test_get_profile_falls_back_to_hobby():
    assert get_profile("nonexistent").name == "hobby"


def test_production_alerts_at_50_80_100():
    p = get_profile("production")
    assert 50 in p.alert_thresholds
    assert 80 in p.alert_thresholds
    assert 100 in p.alert_thresholds


def test_demo_never_kills_silently():
    p = get_profile("demo")
    assert p.kill_silently is False


def test_hobby_kills_at_double():
    p = get_profile("hobby")
    assert p.kill_at_pct == 200


@pytest.mark.parametrize("name", ["hobby", "production", "demo"])
def test_all_profiles_have_at_least_one_alert(name):
    p = get_profile(name)
    assert len(p.alert_thresholds) >= 1
