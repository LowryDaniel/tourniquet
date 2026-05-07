"""Tourniquet CLI — cross-platform entry point.

Usage:
    tourniquet            # same as `tourniquet start`
    tourniquet start      # init config, open browser, run server
    tourniquet init       # init config dir only
    tourniquet add-key    # interactive key add
    tourniquet status     # list keys with today's spend
    tourniquet lift KEY   # lift key cap
    tourniquet --version  # print version
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path

from tourniquet import __version__


# ── Helpers ────────────────────────────────────────────────────────────────────

def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _generate_secret_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _patch_env_value(lines: list[str], key: str, generator) -> tuple[list[str], bool]:
    out: list[str] = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key and not v.strip():
            out.append(f"{key}={generator()}")
            changed = True
        else:
            out.append(line)
    return out, changed


def _init_config_dir(config_dir: Path) -> None:
    """Ensure config dir exists and .env has generated keys."""
    config_dir.mkdir(parents=True, exist_ok=True)
    env_path = config_dir / ".env"

    if not env_path.exists():
        # Find .env.example bundled with the package
        pkg_root = Path(__file__).resolve().parent
        # Walk up from src/tourniquet looking for .env.example
        example = None
        for parent in [pkg_root, pkg_root.parent, pkg_root.parent.parent]:
            candidate = parent / ".env.example"
            if candidate.exists():
                example = candidate
                break
        if example is None:
            # Minimal fallback — just the required vars
            env_path.write_text(
                "DATABASE_URL=sqlite+aiosqlite:///./tourniquet_dev.db\n"
                "FERNET_KEY=\n"
                "SECRET_KEY=\n"
            )
        else:
            env_path.write_text(example.read_text(encoding="utf-8"))
        print(f"  Created {env_path}")

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=False)
    lines, fernet_changed = _patch_env_value(lines, "FERNET_KEY", _generate_fernet_key)
    lines, secret_changed = _patch_env_value(lines, "SECRET_KEY", _generate_secret_key)
    if fernet_changed or secret_changed:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if fernet_changed:
            print("  Generated FERNET_KEY")
        if secret_changed:
            print("  Generated SECRET_KEY")
    else:
        print("  Keys already present — left untouched")


# ── Subcommand handlers ────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> None:
    config_dir = Path(args.config_dir).expanduser().resolve()
    port: int = args.port

    print(f"  Config dir : {config_dir}")
    _init_config_dir(config_dir)

    # Point pydantic-settings at the config dir by setting env var BEFORE importing settings
    os.environ["TOURNIQUET_CONFIG_DIR"] = str(config_dir)
    # chdir so relative SQLite paths resolve inside config_dir
    os.chdir(config_dir)

    url = f"http://127.0.0.1:{port}/dashboard"
    print(f"\n  Dashboard  : {url}")
    print("  Press Ctrl+C to stop\n")

    if not args.no_browser:
        def _open() -> None:
            import time
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    import uvicorn
    uvicorn.run("tourniquet.main:app", host="127.0.0.1", port=port, log_level="info")


def cmd_init(args: argparse.Namespace) -> None:
    config_dir = Path(args.config_dir).expanduser().resolve()
    print(f"Initialising config dir: {config_dir}")
    _init_config_dir(config_dir)
    print("Done. Run `tourniquet start` to launch the server.")


def cmd_add_key(_args: argparse.Namespace) -> None:
    """Interactive wrapper — delegates to bootstrap_local script logic inline."""
    import asyncio
    import uuid

    import bcrypt
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from tourniquet.config import settings
    from tourniquet.db import engine, get_session
    from tourniquet.models import ApiKey, Base, User
    from tourniquet.billing.formatting import format_money, from_major_units

    anthropic_key = input("Anthropic key (sk-ant-...): ").strip()
    if not anthropic_key.startswith("sk-ant-"):
        print("ERROR: key must start with sk-ant-", file=sys.stderr)
        sys.exit(1)
    email = input("Email (for alerts): ").strip()
    name = input("Key name [claude-local]: ").strip() or "claude-local"
    cap_str = input("Daily cap in USD [5.00]: ").strip() or "5.00"
    cap_cents = from_major_units(float(cap_str), settings.display_currency)

    tq_token = f"tq_{secrets.token_urlsafe(32)}"
    token_hash = bcrypt.hashpw(tq_token.encode(), bcrypt.gensalt()).decode()
    fernet = Fernet(settings.fernet_key.encode())
    encrypted_key = fernet.encrypt(anthropic_key.encode()).decode()

    async def _run() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with get_session() as session:
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if not user:
                user = User(email=email)
                session.add(user)
                await session.flush()
            key = ApiKey(
                name=name,
                tq_token_hash=token_hash,
                anthropic_key_encrypted=encrypted_key,
                profile="hobby",
                daily_cap_usd_cents=cap_cents,
                kill_enabled=True,
                user_id=user.id,
            )
            session.add(key)
            await session.commit()

    asyncio.run(_run())
    print(f"\n  tq_ token: {tq_token}")
    print("  (shown once — store it securely)\n")
    print(f"  Cap: {format_money(cap_cents, settings.display_currency)}/day")
    print("  Run `tourniquet start` to launch the dashboard.")


def cmd_status(_args: argparse.Namespace) -> None:
    import asyncio
    from datetime import date

    from sqlalchemy import select

    from tourniquet.billing.caps import get_today_spend
    from tourniquet.billing.formatting import format_money
    from tourniquet.config import settings
    from tourniquet.db import get_session
    from tourniquet.models import ApiKey

    async def _run() -> None:
        today = date.today()
        async with get_session() as session:
            result = await session.execute(select(ApiKey))
            keys = result.scalars().all()
            if not keys:
                print("No keys registered. Run `tourniquet add-key`.")
                return
            print(f"{'Name':<20} {'Today':>10} {'Cap':>10}")
            print("-" * 44)
            for k in keys:
                spent = await get_today_spend(k.id, today, session)
                cur = settings.display_currency
                print(f"{k.name:<20} {format_money(spent, cur):>10} {format_money(k.daily_cap_usd_cents, cur):>10}")

    asyncio.run(_run())


def cmd_register_url_handler(_args: argparse.Namespace) -> None:
    """Register tourniquet:// as a system URL scheme."""
    from tourniquet.url_handler import register
    register()


def cmd_handle_url(args: argparse.Namespace) -> None:
    """Parse and dispatch a tourniquet:// URL."""
    from tourniquet.url_handler import handle_url
    rc = handle_url(args.url)
    sys.exit(rc)


def cmd_lift(args: argparse.Namespace) -> None:
    import asyncio
    from datetime import date, datetime, timedelta, timezone

    from sqlalchemy import select

    from tourniquet.billing.formatting import format_money
    from tourniquet.config import settings
    from tourniquet.db import get_session
    from tourniquet.models import ApiKey

    async def _run() -> None:
        async with get_session() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.name == args.key)
            )
            key = result.scalar_one_or_none()
            if not key:
                print(f"ERROR: no key named {args.key!r}", file=sys.stderr)
                sys.exit(1)
            now = datetime.now(timezone.utc)
            tomorrow = now.date() + timedelta(days=1)
            expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
            raw = int(key.daily_cap_usd_cents * args.multiplier)
            lifted = min(raw, key.absolute_ceiling_usd_cents)
            key.lifted_cap_usd_cents = lifted
            key.lift_expires_at = expires_at
            await session.commit()
            print(f"Cap lifted to {format_money(lifted, settings.display_currency)} until midnight UTC.")

    asyncio.run(_run())


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Windows: force UTF-8 on stdout so banner characters don't crash cmd.exe
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        prog="tourniquet",
        description="Anthropic API proxy with hard spend caps",
    )
    parser.add_argument("--version", action="version", version=f"tourniquet {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    # start (default)
    p_start = sub.add_parser("start", help="Start the proxy + dashboard")
    p_start.add_argument("--port", type=int, default=8787)
    p_start.add_argument("--no-browser", action="store_true", dest="no_browser")
    p_start.add_argument("--config-dir", default="~/.tourniquet", dest="config_dir")

    # init
    p_init = sub.add_parser("init", help="Initialise config dir only")
    p_init.add_argument("--config-dir", default="~/.tourniquet", dest="config_dir")

    # add-key
    sub.add_parser("add-key", help="Add an Anthropic key interactively")

    # status
    sub.add_parser("status", help="List keys with today's spend")

    # lift
    p_lift = sub.add_parser("lift", help="Lift a key's daily cap")
    p_lift.add_argument("key", help="Key name")
    p_lift.add_argument("--multiplier", type=float, default=2.0)

    # register-url-handler
    sub.add_parser(
        "register-url-handler",
        help="Register tourniquet:// URL scheme (Windows/Linux) or print instructions (macOS)",
    )

    # handle-url
    p_handle = sub.add_parser("handle-url", help="Handle a tourniquet:// URL (called by OS)")
    p_handle.add_argument("url", help="tourniquet:// URL to dispatch")

    args = parser.parse_args()

    # Default: start
    if args.subcommand is None:
        args.subcommand = "start"
        args.port = 8787
        args.no_browser = False
        args.config_dir = "~/.tourniquet"

    dispatch = {
        "start": cmd_start,
        "init": cmd_init,
        "add-key": cmd_add_key,
        "status": cmd_status,
        "lift": cmd_lift,
        "register-url-handler": cmd_register_url_handler,
        "handle-url": cmd_handle_url,
    }
    dispatch[args.subcommand](args)
