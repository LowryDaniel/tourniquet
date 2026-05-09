"""Tests for `_sleep_protection_status` — pmset/systemd-inhibit/powercfg parsers.

Covers M8 (macOS owner attribution must filter by assertion type) and M9
(Linux/Windows must honestly report unknown rather than lying about
"always-on by default on this OS").
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from tourniquet.dashboard.routes import _sleep_protection_status


# ── macOS / pmset ──────────────────────────────────────────────────────────────

# Real-world style fixture: WhatsApp holds the actual PreventUserIdleSystemSleep
# assertion, while Claude/Slack/etc. hold unrelated NoIdleSleepAssertion-style
# assertions. The buggy parser attributed the wrong owner because it matched
# the FIRST `named:` line regardless of which assertion that line belonged to.
_PMSET_OUTPUT_WHATSAPP_HOLDS_ASSERTION = """\
2026-05-09 12:00:00 +0100
Assertion status system-wide:
   PreventUserIdleSystemSleep              1
   NoIdleSleepAssertion                    1

Listed by owning process:
   pid 5042(Claude): NoIdleSleepAssertion named: "Claude background work"
   pid 7891(WhatsApp): PreventUserIdleSystemSleep named: "Camera capture"
   pid 1234(Slack): NoIdleSleepAssertion named: "Slack call"
"""


_PMSET_OUTPUT_CAFFEINATE_HOLDS_ASSERTION = """\
2026-05-09 12:00:00 +0100
Assertion status system-wide:
   PreventUserIdleSystemSleep              1

Listed by owning process:
   pid 9999(caffeinate): PreventUserIdleSystemSleep named: "caffeinate -di tourniquet"
"""


def _fake_run_factory(stdout: str, returncode: int = 0):
    """Return a function that mimics subprocess.run, returning a CompletedProcess-like."""

    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    return _fake_run


def test_pmset_owner_filters_by_assertion_type():
    """The owner attributed must hold the same assertion type we flagged active.

    Regression test for M8: previously the parser walked per-process lines and
    matched the first `named:` line, even if that line described an unrelated
    assertion. With Claude listed before WhatsApp, the buggy code incorrectly
    reported Claude as the wake-lock holder.
    """
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Darwin"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_PMSET_OUTPUT_WHATSAPP_HOLDS_ASSERTION)):
        result = _sleep_protection_status()

    assert result["platform"] == "darwin"
    assert result["active"] is True
    assert result["owner"] == "WhatsApp"


def test_pmset_caffeinate_owner_recognised():
    """When caffeinate holds the assertion, owner must be the literal string 'caffeinate'."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Darwin"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_PMSET_OUTPUT_CAFFEINATE_HOLDS_ASSERTION)):
        result = _sleep_protection_status()

    assert result["platform"] == "darwin"
    assert result["active"] is True
    assert result["owner"] == "caffeinate"


# ── Linux / systemd-inhibit ────────────────────────────────────────────────────

_SYSTEMD_INHIBIT_TOURNIQUET_HOLDING = """\
     Who: tourniquet (UID 1000/dan, PID 4242/tourniquet)
    What: idle:sleep
     Why: cap enforcement
    Mode: block

1 inhibitor listed.
"""


_SYSTEMD_INHIBIT_NO_RELEVANT_LOCK = """\
     Who: GNOME Shell (UID 1000/dan, PID 1234/gnome-shell)
    What: handle-power-key
     Why: GNOME handling power key
    Mode: block

1 inhibitor listed.
"""


def test_linux_systemd_inhibit_active_when_tourniquet_holds_lock():
    """Linux returns active=True only when systemd-inhibit shows a relevant lock."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Linux"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_SYSTEMD_INHIBIT_TOURNIQUET_HOLDING)):
        result = _sleep_protection_status()

    assert result == {"platform": "linux", "active": True, "owner": "systemd-inhibit"}


def test_linux_no_relevant_inhibitor_returns_inactive():
    """When no idle:sleep / tourniquet inhibitor is present, return active=False (honest unknown)."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Linux"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_SYSTEMD_INHIBIT_NO_RELEVANT_LOCK)):
        result = _sleep_protection_status()

    assert result == {"platform": "linux", "active": False, "owner": ""}


def test_linux_systemd_inhibit_missing_returns_inactive():
    """If `systemd-inhibit` isn't installed, fall back to active=False — never lie."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Linux"), \
         patch("tourniquet.dashboard.routes.subprocess.run", side_effect=FileNotFoundError()):
        result = _sleep_protection_status()

    assert result == {"platform": "linux", "active": False, "owner": ""}


def test_linux_systemd_inhibit_timeout_returns_inactive():
    """If `systemd-inhibit` hangs, treat as inactive — never block the dashboard."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Linux"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=["systemd-inhibit"], timeout=2)):
        result = _sleep_protection_status()

    assert result == {"platform": "linux", "active": False, "owner": ""}


# ── Windows / powercfg /requests ──────────────────────────────────────────────

_POWERCFG_SYSTEM_REQUEST_PRESENT = """\
DISPLAY:
None.

SYSTEM:
[PROCESS] \\Device\\HarddiskVolume3\\Program Files\\Tourniquet\\tourniquet.exe
Cap enforcement.

AWAYMODE:
None.

EXECUTION:
None.

PERFBOOST:
None.

ACTIVELOCKSCREEN:
None.
"""


_POWERCFG_NO_SYSTEM_REQUEST = """\
DISPLAY:
None.

SYSTEM:
None.

AWAYMODE:
None.

EXECUTION:
None.

PERFBOOST:
None.

ACTIVELOCKSCREEN:
None.
"""


def test_windows_powercfg_active_when_system_request_present():
    """Windows returns active=True when SYSTEM section lists a wake-lock holder."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Windows"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_POWERCFG_SYSTEM_REQUEST_PRESENT)):
        result = _sleep_protection_status()

    assert result == {"platform": "windows", "active": True, "owner": "system-execution-state"}


def test_windows_powercfg_inactive_when_system_is_none():
    """When `SYSTEM:\\nNone.` is reported, the box can sleep — return active=False."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Windows"), \
         patch("tourniquet.dashboard.routes.subprocess.run",
               side_effect=_fake_run_factory(_POWERCFG_NO_SYSTEM_REQUEST)):
        result = _sleep_protection_status()

    assert result == {"platform": "windows", "active": False, "owner": ""}


def test_windows_powercfg_missing_returns_inactive():
    """If `powercfg` isn't reachable (sandboxed install, missing PATH), fall back honestly."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Windows"), \
         patch("tourniquet.dashboard.routes.subprocess.run", side_effect=FileNotFoundError()):
        result = _sleep_protection_status()

    assert result == {"platform": "windows", "active": False, "owner": ""}


def test_windows_powercfg_oserror_returns_inactive():
    """Permission errors (some installs require admin) must not crash the dashboard."""
    with patch("tourniquet.dashboard.routes.platform.system", return_value="Windows"), \
         patch("tourniquet.dashboard.routes.subprocess.run", side_effect=OSError("Access denied")):
        result = _sleep_protection_status()

    assert result == {"platform": "windows", "active": False, "owner": ""}
