"""Audit log of cap-changing actions.

`record_action()` adds a row to api_key_actions inside the caller's session.
The caller is responsible for committing — so the audit row lands atomically
with the cap mutation it describes. This means both the cap change and the
audit record succeed or fail together, maintaining consistency.

Audit failures must NEVER break user-facing paths (kill, lift, recovery):
failures are logged and swallowed. Better to have a missing audit row than a
broken kill button — the cap changes are what matter operationally.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.models import ApiKeyAction

log = logging.getLogger(__name__)


# Action type constants — keep these in sync with the model docstring.
ACTION_KILL_NOW = "kill_now"
ACTION_LIFT_BY_AMOUNT = "lift_by_amount"
ACTION_LIFT_MODE = "lift_mode"
ACTION_CAP_SET = "cap_set"
ACTION_RECOVERY_OFFERED = "recovery_offered"
ACTION_ALERT_FIRED = "alert_fired"

# Source constants — where the action originated.
SOURCE_SLACK_SOCKET = "slack_socket"
SOURCE_TELEGRAM_POLL = "telegram_poll"
SOURCE_WEB = "web"
SOURCE_CLI = "cli"
SOURCE_PROXY = "proxy"
SOURCE_AUTO = "auto"


async def record_action(
    session: AsyncSession,
    api_key_id: uuid.UUID,
    action: str,
    source: str,
    summary: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the caller's session. Caller commits."""
    try:
        session.add(
            ApiKeyAction(
                api_key_id=api_key_id,
                action=action,
                source=source,
                summary=summary,
                details=details,
            )
        )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "Audit record failed for action=%s source=%s key=%s: %s",
            action,
            source,
            api_key_id,
            exc,
        )
