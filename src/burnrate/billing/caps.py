"""Cap checking and spend tracking.

caps_today uses INSERT ... ON CONFLICT DO UPDATE for atomic increment.
This avoids read-modify-write races on concurrent requests for the same key.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_today_spend(api_key_id: uuid.UUID, today: date, session: AsyncSession) -> int:
    result = await session.execute(
        text("SELECT total_pence FROM caps_today WHERE api_key_id = :kid AND date = :d"),
        {"kid": str(api_key_id), "d": today},
    )
    row = result.first()
    return row[0] if row else 0


def is_over_cap(spent_pence: int, cap_pence: int) -> bool:
    return spent_pence >= cap_pence


async def add_spend(api_key_id: uuid.UUID, today: date, amount_pence: int, session: AsyncSession) -> None:
    """Atomically increment caps_today, inserting the row if it doesn't exist."""
    await session.execute(
        text("""
            INSERT INTO caps_today (api_key_id, date, total_pence)
            VALUES (:kid, :d, :amount)
            ON CONFLICT (api_key_id, date)
            DO UPDATE SET total_pence = caps_today.total_pence + EXCLUDED.total_pence
        """),
        {"kid": str(api_key_id), "d": today, "amount": amount_pence},
    )
