"""Tourniquet key management CLI.

Subcommands: list, show, update, rotate, delete, suggest, stats

Usage:
    python scripts/manage_keys.py list
    python scripts/manage_keys.py show <key-id-or-name>
    python scripts/manage_keys.py update <key-id-or-name> [--cap MAJOR_UNITS] ...
    python scripts/manage_keys.py rotate <key-id-or-name>
    python scripts/manage_keys.py delete <key-id-or-name>
    python scripts/manage_keys.py suggest <key-id-or-name>
    python scripts/manage_keys.py stats <key-id-or-name>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select, func

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tourniquet.billing.formatting import format_money, from_major_units  # noqa: E402
from tourniquet.billing.caps import get_today_spend  # noqa: E402
from tourniquet.config import settings  # noqa: E402
from tourniquet.db import engine, get_session  # noqa: E402
from tourniquet.models import ApiKey, Base, UsageEvent  # noqa: E402

# ── Colour helpers ────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()
_GREEN = "\033[32m" if _TTY else ""
_YELLOW = "\033[33m" if _TTY else ""
_RED = "\033[31m" if _TTY else ""
_BOLD = "\033[1m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""


def _col(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _TTY else text


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _ensure_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _lookup(identifier: str) -> ApiKey:
    """Resolve identifier to an ApiKey. Accepts UUID prefix (8+ chars) or exact name.

    Raises SystemExit(1) on no match or ambiguous match.
    """
    async with get_session() as session:
        # Try exact name first
        result = await session.execute(select(ApiKey).where(ApiKey.name == identifier))
        by_name = result.scalars().all()

        # Try UUID prefix (8+ chars guard against over-broad matches)
        by_prefix: list[ApiKey] = []
        if len(identifier) >= 8:
            all_keys_result = await session.execute(select(ApiKey))
            all_keys = all_keys_result.scalars().all()
            by_prefix = [k for k in all_keys if str(k.id).startswith(identifier)]

        candidates = {k.id: k for k in (by_name + by_prefix)}.values()
        candidates = list(candidates)

        if not candidates:
            print(f"ERROR: no key found matching {identifier!r}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print(f"ERROR: ambiguous identifier {identifier!r}. Candidates:", file=sys.stderr)
            for k in candidates:
                print(f"  {str(k.id)[:8]}  {k.name}", file=sys.stderr)
            sys.exit(1)

        # Re-fetch with a fresh session so the caller can use the object
        key = candidates[0]
        await session.refresh(key)
        # Detach to use outside session — load all needed attrs eagerly
        key_id = key.id
        key_copy = await session.get(ApiKey, key_id)
        return key_copy


# ── Table renderer ────────────────────────────────────────────────────────────


def _pad(s: str, w: int) -> str:
    return s[:w].ljust(w)


def _table(rows: list[list[str]], headers: list[str]) -> str:
    col_count = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:col_count]):
            widths[i] = max(widths[i], len(str(cell)))

    sep = "  "
    header_line = sep.join(_col(_pad(h, widths[i]), _BOLD) for i, h in enumerate(headers))
    divider = sep.join("-" * widths[i] for i in range(col_count))
    data_lines = [
        sep.join(_pad(str(row[i]) if i < len(row) else "", widths[i]) for i in range(col_count))
        for row in rows
    ]
    return "\n".join([header_line, divider] + data_lines)


# ── Subcommands ───────────────────────────────────────────────────────────────


async def _cmd_list() -> None:
    await _ensure_schema()
    today = date.today()
    currency = settings.display_currency

    async with get_session() as session:
        result = await session.execute(select(ApiKey).order_by(ApiKey.created_at))
        keys = result.scalars().all()

    if not keys:
        print("No keys found.")
        return

    rows = []
    for k in keys:
        async with get_session() as session:
            spent = await get_today_spend(k.id, today, session)
        rows.append(
            [
                str(k.id)[:8],
                k.name,
                format_money(k.daily_cap_usd_cents, currency),
                format_money(spent, currency),
                k.profile,
                "yes" if k.kill_enabled else "no",
                getattr(k, "auto_tune_mode", "off"),
                k.created_at.strftime("%Y-%m-%d") if k.created_at else "",
            ]
        )

    print(
        _table(
            rows,
            ["ID(short)", "Name", "Cap", "Spent today", "Profile", "Kill", "Auto-tune", "Created"],
        )
    )


async def _cmd_show(identifier: str) -> None:
    await _ensure_schema()
    currency = settings.display_currency
    k = await _lookup(identifier)
    today = date.today()
    async with get_session() as session:
        spent_today = await get_today_spend(k.id, today, session)

        # Last 7 days spend
        since = datetime.now(timezone.utc) - timedelta(days=7)
        result = await session.execute(
            select(func.coalesce(func.sum(UsageEvent.cost_usd_cents), 0))
            .where(UsageEvent.api_key_id == k.id)
            .where(UsageEvent.created_at >= since)
        )
        week_spend = result.scalar() or 0

    print(f"\n{_col('Key details', _BOLD)}: {k.name}")
    print("-" * 50)
    print(f"  ID              : {k.id}")
    print(f"  Name            : {k.name}")
    print(f"  Profile         : {k.profile}")
    print(f"  Daily cap       : {format_money(k.daily_cap_usd_cents, currency)}")
    print(f"  Kill enabled    : {k.kill_enabled}")
    print(f"  Auto-tune       : {getattr(k, 'auto_tune_mode', 'off')}")
    if hasattr(k, "absolute_ceiling_usd_cents") and k.absolute_ceiling_usd_cents:
        print(f"  Abs ceiling     : {format_money(k.absolute_ceiling_usd_cents, currency)}")
    print(f"  Alert email     : {k.alert_email or '(none)'}")
    print(f"  Created         : {k.created_at}")
    print(f"\n  Spent today     : {format_money(spent_today, currency)}")
    print(f"  Spent (7d)      : {format_money(week_spend, currency)}")
    # Admin key fingerprint placeholder — populated if sibling agent extends model
    if hasattr(k, "admin_key_fingerprint") and k.admin_key_fingerprint:
        print(f"  Admin key fp    : {k.admin_key_fingerprint}")
    print()


async def _cmd_update(
    identifier: str,
    cap: float | None,
    currency: str | None,
    profile: str | None,
    kill_enabled: bool | None,
    auto_tune: str | None,
    alert_email: str | None,
    ceiling: float | None,
) -> None:
    await _ensure_schema()
    display_currency = settings.display_currency
    k = await _lookup(identifier)

    # Snapshot before
    before = {
        "cap": k.daily_cap_usd_cents,
        "profile": k.profile,
        "kill_enabled": k.kill_enabled,
        "auto_tune_mode": getattr(k, "auto_tune_mode", "off"),
        "alert_email": k.alert_email,
        "absolute_ceiling_usd_cents": getattr(k, "absolute_ceiling_usd_cents", None),
    }

    async with get_session() as session:
        db_key = await session.get(ApiKey, k.id)

        if cap is not None:
            cur = currency or display_currency
            db_key.daily_cap_usd_cents = from_major_units(cap, cur)
        if profile is not None:
            db_key.profile = profile
        if kill_enabled is not None:
            db_key.kill_enabled = kill_enabled
        if auto_tune is not None:
            if hasattr(db_key, "auto_tune_mode"):
                db_key.auto_tune_mode = auto_tune
            else:
                print("WARNING: auto_tune_mode column not yet migrated; skipping.", file=sys.stderr)
        if alert_email is not None:
            db_key.alert_email = alert_email
        if ceiling is not None:
            cur = currency or display_currency
            ceil_cents = from_major_units(ceiling, cur)
            if hasattr(db_key, "absolute_ceiling_usd_cents"):
                db_key.absolute_ceiling_usd_cents = ceil_cents
            else:
                print(
                    "WARNING: absolute_ceiling_usd_cents column not yet migrated; skipping.",
                    file=sys.stderr,
                )

        await session.commit()
        await session.refresh(db_key)
        after_key = db_key

    after = {
        "cap": after_key.daily_cap_usd_cents,
        "profile": after_key.profile,
        "kill_enabled": after_key.kill_enabled,
        "auto_tune_mode": getattr(after_key, "auto_tune_mode", "off"),
        "alert_email": after_key.alert_email,
        "absolute_ceiling_usd_cents": getattr(after_key, "absolute_ceiling_usd_cents", None),
    }

    print(f"\nUpdated key: {k.name}")
    print(f"{'Field':<32}  {'Before':<20}  {'After'}")
    print("-" * 70)
    for field in before:
        b = str(before[field])
        a = str(after[field])
        if field in ("cap", "absolute_ceiling_usd_cents") and before[field] is not None:
            b = format_money(before[field], display_currency) if before[field] else "(none)"
            a = format_money(after[field], display_currency) if after[field] else "(none)"
        marker = _col("*", _GREEN) if b != a else " "
        print(f"  {marker} {field:<29}  {b:<20}  {a}")
    print()


async def _cmd_rotate(identifier: str) -> None:
    await _ensure_schema()
    k = await _lookup(identifier)

    raw_token = f"tq_{secrets.token_urlsafe(32)}"
    new_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()

    async with get_session() as session:
        db_key = await session.get(ApiKey, k.id)
        db_key.tq_token_hash = new_hash
        await session.commit()

    print(f"\nRotated token for key: {_col(k.name, _BOLD)}")
    print("Old token is IMMEDIATELY invalid.\n")
    print(_col("New token (shown ONCE — copy it now):", _YELLOW))
    print(f"\n  {raw_token}\n")


async def _cmd_delete(identifier: str) -> None:
    await _ensure_schema()
    k = await _lookup(identifier)

    confirm = input(f"Type the key name to confirm: ").strip()
    if confirm != k.name:
        print("Confirmation did not match. Aborting.", file=sys.stderr)
        sys.exit(1)

    async with get_session() as session:
        db_key = await session.get(ApiKey, k.id)
        await session.delete(db_key)
        await session.commit()

    print(f"\nDeleted key {_col(k.name, _RED)} and all associated usage events.")


async def _cmd_suggest(identifier: str) -> None:
    await _ensure_schema()
    k = await _lookup(identifier)
    currency = settings.display_currency

    since = datetime.now(timezone.utc) - timedelta(days=14)
    async with get_session() as session:
        result = await session.execute(
            select(
                func.date(UsageEvent.created_at).label("day"),
                func.sum(UsageEvent.cost_usd_cents).label("total"),
            )
            .where(UsageEvent.api_key_id == k.id)
            .where(UsageEvent.created_at >= since)
            .group_by(func.date(UsageEvent.created_at))
            .order_by(func.date(UsageEvent.created_at))
        )
        rows = result.all()

    if not rows:
        print("No usage data in the last 14 days. Cannot suggest a cap.")
        return

    daily_totals = [r.total for r in rows]

    try:
        from tourniquet.billing.suggestions import suggest_from_history  # type: ignore[import]

        suggestion = suggest_from_history(
            daily_totals_usd_cents=daily_totals,
            current_cap_usd_cents=k.daily_cap_usd_cents,
            absolute_ceiling_usd_cents=getattr(k, "absolute_ceiling_usd_cents", None) or 0,
        )
    except ImportError:
        print("billing.suggestions not yet available (sibling agent in progress).")
        return

    avg = sum(daily_totals) / len(daily_totals)
    sorted_totals = sorted(daily_totals)
    p95_idx = min(int(len(sorted_totals) * 0.95), len(sorted_totals) - 1)
    p95 = sorted_totals[p95_idx]
    maximum = max(daily_totals)

    print(f"\nLast 14 days for {_col(k.name, _BOLD)}:")
    print(f"  avg  = {format_money(int(avg), currency)}")
    print(f"  p95  = {format_money(int(p95), currency)}")
    print(f"  max  = {format_money(maximum, currency)}")
    print(f"\n  Current cap     : {format_money(k.daily_cap_usd_cents, currency)}")
    print(f"  Suggested cap   : {format_money(suggestion.suggested_cap_usd_cents, currency)}")

    if suggestion.suggested_cap_usd_cents == k.daily_cap_usd_cents:
        print("\n  Current cap looks optimal.")
        return

    answer = input("\n  Apply suggested cap? [y/N/edit] ").strip().lower()
    if answer == "y":
        async with get_session() as session:
            db_key = await session.get(ApiKey, k.id)
            db_key.daily_cap_usd_cents = suggestion.suggested_cap_usd_cents
            await session.commit()
        print(f"  Applied: {format_money(suggestion.suggested_cap_usd_cents, currency)}")
    elif answer == "edit":
        raw = input("  Enter new cap in major units: ").strip()
        try:
            new_cents = from_major_units(float(raw), currency)
        except (ValueError, TypeError):
            print("Invalid amount. Aborting.", file=sys.stderr)
            sys.exit(1)
        async with get_session() as session:
            db_key = await session.get(ApiKey, k.id)
            db_key.daily_cap_usd_cents = new_cents
            await session.commit()
        print(f"  Applied: {format_money(new_cents, currency)}")
    else:
        print("  No changes made.")


async def _cmd_lift(
    identifier: str,
    multiplier: float | None,
    to_amount: float | None,
    to_ceiling: bool,
    currency: str | None,
    until_midnight: bool,
    for_hours: float | None,
    to_time: str | None,
) -> None:
    """Temporarily raise the daily cap for a key (direct DB write, no HTTP)."""
    import re

    await _ensure_schema()
    display_currency = settings.display_currency
    k = await _lookup(identifier)

    now = datetime.now(timezone.utc)

    # Determine lifted amount
    if to_ceiling:
        lifted_cents = k.absolute_ceiling_usd_cents
    elif to_amount is not None:
        cur = currency or display_currency
        lifted_cents = from_major_units(to_amount, cur)
    else:
        mult = multiplier if multiplier is not None else 2.0
        lifted_cents = int(k.daily_cap_usd_cents * mult)

    # Clamp to ceiling
    clamped = lifted_cents > k.absolute_ceiling_usd_cents
    lifted_cents = min(lifted_cents, k.absolute_ceiling_usd_cents)

    # Determine expiry
    if for_hours is not None:
        expires_at = now + timedelta(hours=for_hours)
    elif to_time is not None:
        m = re.match(r"^(\d{1,2}):(\d{2})$", to_time)
        if not m:
            print("ERROR: --to-time must be HH:MM", file=sys.stderr)
            sys.exit(1)
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        expires_at = candidate
    else:
        # Default: until midnight UTC
        tomorrow = now.date() + timedelta(days=1)
        expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

    async with get_session() as session:
        db_key = await session.get(ApiKey, k.id)
        db_key.lifted_cap_usd_cents = lifted_cents
        db_key.lift_expires_at = expires_at
        await session.commit()

    currency_out = currency or display_currency
    clamp_note = f"  {_col('(clamped to ceiling)', _YELLOW)}" if clamped else ""
    print(f"\nLifted cap for {_col(k.name, _BOLD)}")
    print(f"  Base cap   : {format_money(k.daily_cap_usd_cents, currency_out)}")
    print(f"  Lifted cap : {format_money(lifted_cents, currency_out)}{clamp_note}")
    print(f"  Expires at : {expires_at.isoformat()}")
    print(f"  Ceiling    : {format_money(k.absolute_ceiling_usd_cents, currency_out)}")
    print()


async def _cmd_unlift(identifier: str) -> None:
    """Clear a cap lift early, restoring the base daily cap immediately."""
    await _ensure_schema()
    k = await _lookup(identifier)

    async with get_session() as session:
        db_key = await session.get(ApiKey, k.id)
        db_key.lifted_cap_usd_cents = None
        db_key.lift_expires_at = None
        await session.commit()

    currency = settings.display_currency
    print(f"\nUnlifted cap for {_col(k.name, _BOLD)}")
    print(f"  Restored cap : {format_money(k.daily_cap_usd_cents, currency)}")
    print(f"  Lift cleared.")
    print()


async def _cmd_stats(identifier: str) -> None:
    await _ensure_schema()
    k = await _lookup(identifier)
    currency = settings.display_currency
    today = date.today()
    since_dt = datetime.now(timezone.utc) - timedelta(days=14)

    async with get_session() as session:
        result = await session.execute(
            select(
                func.date(UsageEvent.created_at).label("day"),
                func.count(UsageEvent.id).label("requests"),
                func.sum(UsageEvent.input_tokens).label("input_tokens"),
                func.sum(UsageEvent.output_tokens).label("output_tokens"),
                func.sum(UsageEvent.cost_usd_cents).label("cost"),
            )
            .where(UsageEvent.api_key_id == k.id)
            .where(UsageEvent.created_at >= since_dt)
            .group_by(func.date(UsageEvent.created_at))
            .order_by(func.date(UsageEvent.created_at))
        )
        rows = result.all()

    print(f"\nStats for {_col(k.name, _BOLD)} — last 14 days:")
    print(f"Daily cap: {format_money(k.daily_cap_usd_cents, currency)}\n")

    cap = k.daily_cap_usd_cents
    table_rows = []
    totals = [0, 0, 0, 0]
    for r in rows:
        cost = r.cost or 0
        pct = f"{(cost / cap * 100):.1f}%" if cap > 0 else "n/a"
        table_rows.append(
            [
                str(r.day),
                str(r.requests or 0),
                str(r.input_tokens or 0),
                str(r.output_tokens or 0),
                format_money(cost, currency),
                pct,
            ]
        )
        totals[0] += r.requests or 0
        totals[1] += r.input_tokens or 0
        totals[2] += r.output_tokens or 0
        totals[3] += cost

    headers = ["Date", "Requests", "Input tok", "Output tok", "Cost", "% of cap"]
    print(_table(table_rows, headers))

    total_pct = f"{(totals[3] / cap * 100):.1f}%" if cap > 0 else "n/a"
    print("\n" + "=" * 60)
    print(
        f"  {'TOTAL':<12}  {totals[0]:<10}  {totals[1]:<11}  {totals[2]:<11}  "
        f"{format_money(totals[3], currency):<10}  {total_pct}"
    )
    print()


# ── Argument parser ───────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_keys.py",
        description="Tourniquet API key management CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # list
    sub.add_parser("list", help="List all keys")

    # show
    p_show = sub.add_parser("show", help="Show full key details")
    p_show.add_argument("key", metavar="KEY-ID-OR-NAME")

    # update
    p_update = sub.add_parser("update", help="Update key fields")
    p_update.add_argument("key", metavar="KEY-ID-OR-NAME")
    p_update.add_argument("--cap", type=float, help="New daily cap in major currency units")
    p_update.add_argument(
        "--currency", help="Currency code for --cap (default: settings.display_currency)"
    )
    p_update.add_argument(
        "--profile", choices=["standard", "strict", "monitor"], help="Billing profile"
    )
    kill_group = p_update.add_mutually_exclusive_group()
    kill_group.add_argument(
        "--kill-enabled", dest="kill_enabled", action="store_true", default=None
    )
    kill_group.add_argument("--kill-disabled", dest="kill_enabled", action="store_false")
    p_update.add_argument(
        "--auto-tune", choices=["off", "suggest", "creep"], dest="auto_tune", help="Auto-tune mode"
    )
    p_update.add_argument("--alert-email", dest="alert_email", help="Alert email address")
    p_update.add_argument(
        "--ceiling", type=float, help="Absolute spending ceiling in major currency units"
    )

    # rotate
    p_rotate = sub.add_parser("rotate", help="Generate a new tq_ token")
    p_rotate.add_argument("key", metavar="KEY-ID-OR-NAME")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a key (with confirmation)")
    p_delete.add_argument("key", metavar="KEY-ID-OR-NAME")

    # suggest
    p_suggest = sub.add_parser("suggest", help="Suggest a cap from usage history")
    p_suggest.add_argument("key", metavar="KEY-ID-OR-NAME")

    # stats
    p_stats = sub.add_parser("stats", help="Last 14 days usage breakdown")
    p_stats.add_argument("key", metavar="KEY-ID-OR-NAME")

    # lift
    p_lift = sub.add_parser("lift", help="Temporarily raise the daily cap")
    p_lift.add_argument("key", metavar="KEY-ID-OR-NAME")
    lift_amount_group = p_lift.add_mutually_exclusive_group()
    lift_amount_group.add_argument(
        "--multiplier", type=float, default=None, help="Multiply base cap by N (default 2)"
    )
    lift_amount_group.add_argument(
        "--to",
        type=float,
        dest="to_amount",
        default=None,
        metavar="AMOUNT",
        help="Lift to specific amount in major currency units",
    )
    lift_amount_group.add_argument(
        "--to-ceiling", action="store_true", default=False, help="Lift to absolute ceiling"
    )
    p_lift.add_argument(
        "--currency", help="Currency code for --to (default: settings.display_currency)"
    )
    lift_time_group = p_lift.add_mutually_exclusive_group()
    lift_time_group.add_argument(
        "--until", choices=["midnight"], default=None, help="Lift until midnight UTC (default)"
    )
    lift_time_group.add_argument(
        "--for-hours",
        type=float,
        dest="for_hours",
        default=None,
        metavar="N",
        help="Lift for N hours from now",
    )
    lift_time_group.add_argument(
        "--to-time",
        dest="to_time",
        default=None,
        metavar="HH:MM",
        help="Lift until HH:MM today (or tomorrow if past)",
    )

    # unlift
    p_unlift = sub.add_parser("unlift", help="Clear a cap lift early")
    p_unlift.add_argument("key", metavar="KEY-ID-OR-NAME")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(_cmd_list())

    elif args.command == "show":
        asyncio.run(_cmd_show(args.key))

    elif args.command == "update":
        # kill_enabled may still be None if neither flag passed — detect via namespace
        kill = None
        if "--kill-enabled" in sys.argv:
            kill = True
        elif "--kill-disabled" in sys.argv:
            kill = False
        asyncio.run(
            _cmd_update(
                identifier=args.key,
                cap=args.cap,
                currency=args.currency,
                profile=args.profile,
                kill_enabled=kill,
                auto_tune=args.auto_tune,
                alert_email=args.alert_email,
                ceiling=args.ceiling,
            )
        )

    elif args.command == "rotate":
        asyncio.run(_cmd_rotate(args.key))

    elif args.command == "delete":
        asyncio.run(_cmd_delete(args.key))

    elif args.command == "suggest":
        asyncio.run(_cmd_suggest(args.key))

    elif args.command == "stats":
        asyncio.run(_cmd_stats(args.key))

    elif args.command == "lift":
        asyncio.run(
            _cmd_lift(
                identifier=args.key,
                multiplier=args.multiplier,
                to_amount=args.to_amount,
                to_ceiling=args.to_ceiling,
                currency=args.currency,
                until_midnight=args.until == "midnight" if args.until else True,
                for_hours=args.for_hours,
                to_time=args.to_time,
            )
        )

    elif args.command == "unlift":
        asyncio.run(_cmd_unlift(args.key))


if __name__ == "__main__":
    main()
