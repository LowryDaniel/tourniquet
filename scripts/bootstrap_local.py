"""Local PoC bootstrap.

Creates SQLite schema, registers one Anthropic key, prints the tq_* token to use
as the bearer for the proxy. Idempotent: re-running with the same email reuses
the user but always creates a NEW api_key (and rotates the printed token).

Usage:
    export ANTHROPIC_API_KEY=sk-ant-xxx
    python scripts/bootstrap_local.py \\
        --email you@example.com \\
        --name "claude-code-local" \\
        --cap 10.00 \\
        --currency GBP

Defaults: cap = 5.00 (in the deployment currency), kill_enabled = True, profile = hobby.

The tq_ token is shown ONCE here. It's bcrypt-hashed in the DB; we cannot recover
it. Lose it = create a new key row.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import sys
import uuid

import bcrypt
from cryptography.fernet import Fernet
from sqlalchemy import select

# Make `tourniquet` importable when run from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tourniquet.billing.formatting import format_money, from_major_units  # noqa: E402
from tourniquet.config import settings  # noqa: E402
from tourniquet.db import get_session  # noqa: E402
from tourniquet.models import ApiKey, User  # noqa: E402


def _make_tq_token() -> str:
    return f"tq_{secrets.token_urlsafe(32)}"


def _ensure_schema() -> None:
    from tourniquet.migrate import upgrade_to_head

    upgrade_to_head(settings.database_url)


async def _bootstrap(
    email: str, name: str, cap_usd_cents: int, anthropic_key: str, auto_tune_mode: str = "suggest"
) -> str:
    _ensure_schema()

    fernet = Fernet(settings.fernet_key.encode())
    encrypted_anthropic = fernet.encrypt(anthropic_key.encode()).decode()

    raw_token = _make_tq_token()
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()

    async with get_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(id=uuid.uuid4(), email=email)
            session.add(user)
            await session.flush()

        key_kwargs: dict = dict(
            id=uuid.uuid4(),
            user_id=user.id,
            name=name,
            tq_token_hash=token_hash,
            anthropic_key_encrypted=encrypted_anthropic,
            profile="standard",
            daily_cap_usd_cents=cap_usd_cents,
            kill_enabled=True,
            alert_email=email,
        )
        # Set auto_tune_mode if the column exists on the model
        from tourniquet.models import ApiKey as _AK  # noqa: N814

        if hasattr(_AK, "auto_tune_mode"):
            key_kwargs["auto_tune_mode"] = auto_tune_mode
        api_key = ApiKey(**key_kwargs)
        session.add(api_key)
        await session.commit()

    return raw_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True, help="Owner email (used for alerts)")
    parser.add_argument("--name", default="claude-code-local", help="Friendly key name")
    parser.add_argument(
        "--cap",
        type=float,
        default=5.00,
        help=(
            "Daily spend cap in major units of the deployment currency "
            "(e.g. 10.00 = $10 / £10). Default: 5.00."
        ),
    )
    parser.add_argument(
        "--currency",
        default=None,
        help=(
            "Currency code for the cap amount (e.g. USD, GBP, EUR). "
            "Defaults to DISPLAY_CURRENCY setting."
        ),
    )
    args = parser.parse_args()

    currency = args.currency or settings.display_currency
    cap_usd_cents = from_major_units(args.cap, currency)

    # ── Admin-key prompt (optional): suggest cap from real usage history ──────
    print()
    print("Optional: paste an admin key (sk-ant-admin-...) and I'll look up your last")
    print("14 days and suggest sensible caps. Press Enter to skip.")
    try:
        admin_key = getpass.getpass("Admin key (hidden): ").strip()
    except (KeyboardInterrupt, EOFError):
        admin_key = ""

    if admin_key:
        if not admin_key.startswith("sk-ant-admin-"):
            print("ERROR: Admin keys start with sk-ant-admin-", file=sys.stderr)
            sys.exit(1)

        try:
            from tourniquet.anthropic_admin import fetch_cost_history  # noqa: E402
            from tourniquet.billing.suggestions import (
                suggest_from_history,  # type: ignore[import]  # noqa: E402
            )

            daily_costs = asyncio.run(fetch_cost_history(admin_key, days=14))
            # CRITICAL: zero out the admin key immediately after use
            del admin_key

            if daily_costs:
                daily_totals = [dc.usd_cents for dc in daily_costs]
                suggestion = suggest_from_history(
                    daily_totals_usd_cents=daily_totals,
                    current_cap_usd_cents=cap_usd_cents,
                    absolute_ceiling_usd_cents=0,
                )
                avg_cents = int(sum(daily_totals) / len(daily_totals))
                sorted_totals = sorted(daily_totals)
                p95_idx = min(int(len(sorted_totals) * 0.95), len(sorted_totals) - 1)
                p95_cents = sorted_totals[p95_idx]
                max_cents = max(daily_totals)
                suggested = suggestion.suggested_cap_usd_cents

                print()
                print(
                    f"  Your last 14 days:  avg={format_money(avg_cents, currency)}"
                    f"  p95={format_money(p95_cents, currency)}"
                    f"  max={format_money(max_cents, currency)}"
                )
                print(
                    f"  Suggested cap: {format_money(suggested, currency)}"
                    f"  (you entered: {format_money(cap_usd_cents, currency)})"
                )
                try:
                    answer = input("  Use suggested cap instead? [Y/n] ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    answer = "n"
                if answer != "n":
                    cap_usd_cents = suggested
                    print(f"  Using suggested cap: {format_money(cap_usd_cents, currency)}")
                else:
                    print(f"  Keeping your cap: {format_money(cap_usd_cents, currency)}")
            else:
                print("  No usage history found; using your --cap value.")

        except ImportError as exc:
            # billing.suggestions not yet available
            del admin_key
            print(f"  (suggestion module not available: {exc}; using --cap value)")
        except Exception as exc:
            # Never leak the key in error output
            try:  # noqa: SIM105 — explicit try/except is clearer than contextlib here
                del admin_key
            except NameError:
                pass
            print(f"  Admin key lookup failed: {exc}. Using --cap value.", file=sys.stderr)
    else:
        # No admin key provided — still zero out the variable
        del admin_key

    # ── Create the key ────────────────────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key or not anthropic_key.startswith("sk-ant-"):
        print(
            "ERROR: ANTHROPIC_API_KEY env var missing or malformed.\n"
            "       Set it before running: export ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        sys.exit(1)

    raw_token = asyncio.run(
        _bootstrap(
            email=args.email,
            name=args.name,
            cap_usd_cents=cap_usd_cents,
            anthropic_key=anthropic_key,
            auto_tune_mode="suggest",
        )
    )

    cap_display = format_money(cap_usd_cents, currency)

    print()
    print("=" * 72)
    print("  Tourniquet local key created")
    print("=" * 72)
    print(f"  Email      : {args.email}")
    print(f"  Key name   : {args.name}")
    print(f"  Daily cap  : {cap_display} ({cap_usd_cents} USD cents stored)")
    print(f"  Currency   : {currency}")
    print("  Kill switch: ENABLED")
    print()
    print("  Bearer token (shown ONCE — copy it now):")
    print()
    print(f"    {raw_token}")
    print()
    print("  Use it like this:")
    print()
    print("    export ANTHROPIC_BASE_URL=http://localhost:8000")
    print(f"    export ANTHROPIC_API_KEY={raw_token}")
    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
