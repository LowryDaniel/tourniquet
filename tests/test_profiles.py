"""Profile definitions — invariants that must hold across all profiles."""

import pytest

from tourniquet.billing.profiles import PROFILES, get_profile


def test_three_profiles_exist():
    assert set(PROFILES.keys()) == {"standard", "strict", "monitor"}


def test_get_profile_falls_back_to_standard():
    assert get_profile("nonexistent").name == "standard"


def test_standard_alert_thresholds():
    p = get_profile("standard")
    assert p.alert_thresholds == [50, 80, 100]


def test_strict_alert_thresholds():
    p = get_profile("strict")
    assert p.alert_thresholds == [100]


def test_monitor_alert_thresholds():
    p = get_profile("monitor")
    assert 50 in p.alert_thresholds
    assert 80 in p.alert_thresholds
    assert 100 in p.alert_thresholds


def test_standard_default_kill_enabled():
    assert get_profile("standard").default_kill_enabled is True


def test_strict_default_kill_enabled():
    assert get_profile("strict").default_kill_enabled is True


def test_monitor_default_kill_disabled():
    assert get_profile("monitor").default_kill_enabled is False


@pytest.mark.parametrize("name", ["standard", "strict", "monitor"])
def test_all_profiles_have_at_least_one_alert(name):
    p = get_profile(name)
    assert len(p.alert_thresholds) >= 1


@pytest.mark.parametrize("name", ["standard", "strict", "monitor"])
def test_all_profiles_have_description(name):
    p = get_profile(name)
    assert p.description
    assert p.pick_me_if
