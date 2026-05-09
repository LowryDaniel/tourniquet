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
    """Return today's total spend in USD cents, or 0 if no row exists."""
    result = await session.execute(
        text("SELECT total_usd_cents FROM caps_today WHERE api_key_id = :kid AND date = :d"),
        {"kid": str(api_key_id), "d": today},
    )
    row = result.first()
    return row[0] if row else 0


def is_over_cap(spent_cents: int, cap_cents: int) -> bool:
    return spent_cents >= cap_cents


async def add_spend(api_key_id: uuid.UUID, today: date, amount_cents: int, session: AsyncSession) -> None:
    """Atomically increment caps_today in USD cents, inserting the row if it doesn't exist.

    Used for the reconciliation path after a reservation: the caller passes
    `actual_cost - reserved_cost`, which can be negative (over-estimate to
    refund) or positive (under-estimate to top up — should be rare since
    reservation already books the worst case).
    """
    await session.execute(
        text("""
            INSERT INTO caps_today (api_key_id, date, total_usd_cents)
            VALUES (:kid, :d, :amount)
            ON CONFLICT (api_key_id, date)
            DO UPDATE SET total_usd_cents = caps_today.total_usd_cents + EXCLUDED.total_usd_cents
        """),
        {"kid": str(api_key_id), "d": today, "amount": amount_cents},
    )


async def reserve_or_reject(
    api_key_id: uuid.UUID,
    today: date,
    amount_cents: int,
    cap_cents: int,
    session: AsyncSession,
) -> bool:
    """Atomic check-and-increment for `caps_today`.

    Returns True if the reservation succeeded (the row was inserted or
    updated within the cap), False if the reservation would push spend over
    the cap. Caller MUST commit the session for the reserve to take effect.

    The two-arm `INSERT ... ON CONFLICT DO UPDATE WHERE` is atomic on both
    Postgres and SQLite (≥3.24, bundled with Python 3.11+):

      - Insert arm: if no row exists for (api_key_id, today) and
        amount_cents <= cap_cents, the new row is inserted with total
        = amount_cents.

      - Update arm: if a row exists, the WHERE predicate gates the update.
        If `existing.total + amount > cap`, the update is skipped — the
        statement returns no row → False.

    The insert arm has its own guard: when the row doesn't exist, we still
    need to refuse a single oversize request that exceeds the cap on its
    own. The trailing WHERE on the SELECT after the statement handles this:
    if amount_cents > cap_cents and no row pre-existed, the row is inserted
    but we treat the post-insert total as the source of truth. We capture
    `total_usd_cents` from RETURNING and reject if it exceeds the cap.

    Implementation note: SQLite's ON CONFLICT DO UPDATE WHERE is supported
    from 3.24+; Python 3.11 ships SQLite ≥3.40 in the bundled module.
    Postgres has supported this since 9.5.
    """
    # Refuse a single oversize request that exceeds the cap on its own,
    # without inserting a row. Otherwise the insert arm of ON CONFLICT
    # would book the row even though it busts the cap. (UPSERT's WHERE
    # only gates the UPDATE arm, not the INSERT arm.)
    if amount_cents > cap_cents:
        # Still check whether an existing row would accommodate it (it
        # won't, since amount alone > cap), but be defensive.
        return False

    result = await session.execute(
        text("""
            INSERT INTO caps_today (api_key_id, date, total_usd_cents)
            VALUES (:kid, :d, :amt)
            ON CONFLICT (api_key_id, date) DO UPDATE
              SET total_usd_cents = caps_today.total_usd_cents + EXCLUDED.total_usd_cents
              WHERE caps_today.total_usd_cents + EXCLUDED.total_usd_cents <= :cap
            RETURNING total_usd_cents
        """),
        {"kid": str(api_key_id), "d": today, "amt": amount_cents, "cap": cap_cents},
    )
    return result.first() is not None
