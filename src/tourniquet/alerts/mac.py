"""Mac OS notification channel via osascript.

mac_notification_style controls the format:
  "text"   — plain text message only (original behaviour)
  "action" — appends a click-action URL tourniquet://lift/<key_id> to the message body.
              Power users can register a URL handler via macOS Automator / Shortcuts:
              1. Open Shortcuts.app → File → New Shortcut
              2. Add "Run Shell Script" action: python /path/to/manage_keys.py lift "$1"
              3. In Shortcuts preferences, enable "Allow Running Scripts"
              4. Open Automator → New Document → Application, filter URL pattern
                 tourniquet://* to invoke the shortcut.
              The URL is included in the notification body; clicking the notification itself
              does not invoke the handler (macOS does not support click-URL from osascript).
  "both"   — includes the action URL AND the literal CLI command in the message body.
             Default. Most useful in development / for power users with a terminal handy.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from tourniquet.config import settings


async def send_mac_notification(title: str, message: str, event: object | None = None) -> None:
    """Display a macOS notification banner.

    No-op on non-Darwin platforms or when ENABLE_MAC_NOTIFICATIONS != "true".
    Inspects settings.mac_notification_style to decide the message format.
    """
    if not sys.platform == "darwin":
        return
    if not settings.enable_mac_notifications:
        return

    style = getattr(settings, "mac_notification_style", "both")
    key_id: str | None = None
    key_name: str | None = None

    if event is not None:
        key_id = getattr(event, "api_key_id", None) or None
        key_name = getattr(event, "api_key_name", None) or None

    if style != "text" and key_id:
        lift_url = f"tourniquet://lift/{key_id}"
        cli_cmd = f"python scripts/manage_keys.py lift {key_name or key_id}"

        if style == "action":
            message = f"{message}\n{lift_url}"
        elif style == "both":
            message = f"{message}\n{lift_url}\n{cli_cmd}"

    title_esc = json.dumps(title)
    message_esc = json.dumps(message)

    script = f"display notification {message_esc} with title {title_esc}"
    await asyncio.to_thread(
        subprocess.run,
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
    )
