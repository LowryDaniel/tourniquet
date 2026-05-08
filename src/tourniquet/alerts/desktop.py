"""Cross-platform desktop notification channel.

Dispatches based on sys.platform:
  darwin  — osascript (existing behaviour)
  win32   — plyer (requires ``pip install plyer>=2.1``)
  linux   — plyer (dispatches to libnotify under the hood)

mac_notification_style controls the message format on all platforms:
  "text"   — plain text message only
  "action" — appends a click-action URL tourniquet://lift/<key_id>
  "both"   — action URL + literal CLI command (default)

Falls back to a no-op if plyer is not installed or the platform is
unrecognised.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from tourniquet.config import settings


def _notifications_enabled() -> bool:
    return (
        settings.enable_mac_notifications == "true"
        or getattr(settings, "enable_desktop_notifications", "") == "true"
    )


def _build_message(message: str, event: object | None) -> str:
    """Return the canonical message + ONE consistent action line.

    macOS notification banners can't host inline buttons, so we append a single
    dashboard URL — same shape on every alert. The dashboard is where all
    actions (lift / kill / bump) live, so this single appendix is enough.

    The body text itself is NEVER modified — it must match what Slack/Telegram/
    JSONL/email show, character for character.
    """
    if event is None:
        return message
    key_id = getattr(event, "api_key_id", None) or None
    if not key_id:
        return message
    dashboard_url = f"{settings.app_base_url}/dashboard/key/{key_id}"
    return f"{message}\n→ {dashboard_url}"


async def send_desktop_notification(
    title: str,
    message: str,
    event: object | None = None,
) -> None:
    """Display a desktop notification banner.

    No-op when desktop notifications are not enabled or cannot fire.
    """
    if not _notifications_enabled():
        return

    message = _build_message(message, event)

    if sys.platform == "darwin":
        title_esc = json.dumps(title)
        message_esc = json.dumps(message)
        script = f"display notification {message_esc} with title {title_esc}"
        await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
        )
        return

    # Windows / Linux — use plyer
    try:
        import plyer  # type: ignore[import]
        await asyncio.to_thread(
            plyer.notification.notify,
            title=title,
            message=message,
            app_name="Tourniquet",
            timeout=10,
        )
    except ImportError:
        # plyer not installed — silent no-op
        pass
    except Exception:
        # Notification daemon unavailable (headless server etc.) — silent no-op
        pass


# Backwards-compat alias (tests + any external code that imported mac.py directly)
async def send_mac_notification(
    title: str,
    message: str,
    event: object | None = None,
) -> None:
    """Alias for send_desktop_notification — preserved for backwards compatibility."""
    await send_desktop_notification(title, message, event)
