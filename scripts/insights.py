"""Tourniquet insights CLI.

Usage:
    python scripts/insights.py <key-id-or-name> [--days 7] [--currency GBP]

Prints a local-only anomaly report to stdout. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tourniquet.analytics.insights import InsightReport, compute_insights  # noqa: E402
from tourniquet.billing.formatting import format_money  # noqa: E402
from tourniquet.config import settings  # noqa: E402
from tourniquet.db import engine, get_session  # noqa: E402
from tourniquet.models import ApiKey, Base  # noqa: E402

# ── Colour helpers ────────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()
_BOLD = "\033[1m" if _TTY else ""
_DIM = "\033[2m" if _TTY else ""
_RESET = "\033[0m" if _TTY else ""

SEP = "=" * 72


# ── Key lookup (mirrors manage_keys._lookup, intentionally copied) ────────────


async def _lookup(identifier: str) -> ApiKey:
    """Resolve identifier to an ApiKey. Accepts UUID prefix (8+ chars) or exact name."""
    async with get_session() as session:
        result = await session.execute(select(ApiKey).where(ApiKey.name == identifier))
        by_name = result.scalars().all()

        by_prefix: list[ApiKey] = []
        if len(identifier) >= 8:
            all_result = await session.execute(select(ApiKey))
            all_keys = all_result.scalars().all()
            by_prefix = [k for k in all_keys if str(k.id).startswith(identifier)]

        candidates = list({k.id: k for k in (by_name + by_prefix)}.values())

        if not candidates:
            print(f"ERROR: no key found matching {identifier!r}", file=sys.stderr)
            sys.exit(1)
        if len(candidates) > 1:
            print(f"ERROR: ambiguous identifier {identifier!r}. Candidates:", file=sys.stderr)
            for k in candidates:
                print(f"  {str(k.id)[:8]}  {k.name}", file=sys.stderr)
            sys.exit(1)

        key_id = candidates[0].id
        return await session.get(ApiKey, key_id)


# ── Formatting helpers ────────────────────────────────────────────────────────


def _fmt(cents: int, currency: str) -> str:
    return format_money(cents, currency)


def _pct_str(pct: float) -> str:
    return f"{pct:.0f}%"


def _print_breakdown(rows, currency: str, show_requests: bool = True) -> None:
    if not rows:
        print("    (no data)")
        return
    for r in rows:
        cost_str = _fmt(r.cost_cents, currency).rjust(10)
        pct_str = _pct_str(r.pct_of_total).rjust(4)
        if show_requests:
            req_str = f"({r.request_count:>3} requests)"
            print(f"    {r.name:<32}  {cost_str}  {pct_str}   {req_str}")
        else:
            print(f"    {r.name:<32}  {cost_str}  {pct_str}")


# ── Main report ───────────────────────────────────────────────────────────────


def _render_report(report: InsightReport, currency: str) -> None:
    print()
    print(f"{_BOLD}Insights — {report.api_key_name} — last {report.days} days{_RESET}")
    print(SEP)
    print()
    print(
        f"  Total spent:      {_fmt(report.total_usd_cents, currency)}  "
        f"({report.request_count} requests)"
    )
    print(
        f"  Cap-hit days:     {report.cap_hit_days}   "
        f"(vs {report.cap_hit_days_prior} the prior {report.days} days)"
    )
    print()

    print(f"  {_BOLD}By model:{_RESET}")
    _print_breakdown(report.by_model, currency)
    print()

    if report.by_caller:
        print(f"  {_BOLD}By caller (user-agent):{_RESET}")
        _print_breakdown(report.by_caller, currency)
        print()

    if report.by_metadata_user_id:
        print(f"  {_BOLD}By metadata.user_id:{_RESET}")
        _print_breakdown(report.by_metadata_user_id, currency, show_requests=False)
        print()

    if report.hottest_hour:
        from tourniquet.analytics.insights import _WEEKDAY_NAMES

        h = report.hottest_hour
        wday = _WEEKDAY_NAMES[h.weekday]
        mult = f"{h.z_score:.0f}×" if h.z_score < 100 else "far above"
        print(
            f"  Hottest hour: {wday} {h.hour:02d}:00–{h.hour + 1:02d}:00 = "
            f"{_fmt(h.cost_cents, currency)} ({mult} usual baseline)"
        )
        print()

    if report.biggest_request:
        r = report.biggest_request
        ts = r.created_at
        ts_str = ts.strftime("%a %H:%M") if ts and hasattr(ts, "strftime") else "unknown"
        print(f"  {_BOLD}Biggest single request:{_RESET}")
        print(
            f"    {ts_str}  {r.input_tokens:>7,} input  {r.output_tokens:>6,} output  "
            f"{_fmt(r.cost_usd_cents, currency)}"
        )
        print(f"    model={r.model}")
        muid = getattr(r, "metadata_user_id", None)
        if muid:
            print(f"    metadata.user_id={muid}")
        print(f"    → {report.biggest_request_pct:.0f}% of the {report.days}-day total")
        print()

    if report.suggestions:
        print(f"  {_BOLD}Suggestions:{_RESET}")
        for s in report.suggestions:
            print(f"    • {s}")
        print()

    print(f"  {_DIM}All data computed locally. Nothing left this machine.{_RESET}")
    print(SEP)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────


async def _run(identifier: str, days: int, currency: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    key = await _lookup(identifier)
    async with get_session() as session:
        report = await compute_insights(key.id, days, session)

    _render_report(report, currency)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="insights.py",
        description="Local token burn analysis — nothing leaves this machine.",
    )
    parser.add_argument("key", metavar="KEY-ID-OR-NAME", help="Key name or UUID prefix (8+ chars)")
    parser.add_argument("--days", type=int, default=7, help="Analysis window in days (default: 7)")
    parser.add_argument(
        "--currency",
        default=settings.display_currency,
        help=f"Display currency (default: {settings.display_currency})",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.key, args.days, args.currency))


if __name__ == "__main__":
    main()
