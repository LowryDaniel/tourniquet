"""Tourniquet CLI — cross-platform entry point.

Usage:
    tourniquet            # same as `tourniquet start`
    tourniquet start      # init config, open browser, run server
    tourniquet init       # init config dir only
    tourniquet add-key    # interactive key add
    tourniquet status     # list keys with today's spend
    tourniquet lift KEY   # lift key cap
    tourniquet test       # send a real test request through the proxy
    tourniquet test-alerts  # fire synthetic alert through all channels
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


def _lookup_key_by_name(name: str):
    """Look up an ApiKey by exact name. Returns None if not found.

    Used by `test-alerts --key NAME` to bind the synthetic alert to a real
    key — so the alert message shows that key's actual cap, and in-app taps
    (e.g., Telegram bump buttons) persist back to its `lifted_cap_usd_cents`,
    actually mutating the real cap. Without this binding, --key only affects
    the human-readable label in the alert, not the backend.
    """
    import asyncio as _asyncio

    from sqlalchemy import select

    from tourniquet.db import get_session
    from tourniquet.models import ApiKey

    async def _run():
        async with get_session() as s:
            return (
                await s.execute(select(ApiKey).where(ApiKey.name == name))
            ).scalar_one_or_none()

    try:
        return _asyncio.run(_run())
    except Exception:
        return None


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
                profile="standard",
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


def cmd_test(args: argparse.Namespace) -> None:
    """Send one small request through the proxy and pretty-print what happened.

    Reads tq_ token from --token or $ANTHROPIC_API_KEY. Reads proxy URL from
    --base-url or $ANTHROPIC_BASE_URL or defaults to http://127.0.0.1:8787.
    """
    import json as _json

    import httpx

    is_tty = sys.stdout.isatty()
    GREEN = "\033[32m" if is_tty else ""
    RED = "\033[31m" if is_tty else ""
    YELLOW = "\033[33m" if is_tty else ""
    DIM = "\033[2m" if is_tty else ""
    BOLD = "\033[1m" if is_tty else ""
    RESET = "\033[0m" if is_tty else ""

    token = args.token or os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = args.base_url or os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    if not token.startswith("tq_"):
        print(f"{RED}✗ No Tourniquet token found.{RESET}")
        print(f"  Pass {BOLD}--token tq_...{RESET} or {BOLD}export ANTHROPIC_API_KEY=tq_...{RESET}")
        sys.exit(1)

    payload = {
        "model": args.model,
        "max_tokens": 50,
        "messages": [{"role": "user", "content": args.message}],
    }

    print(f"{DIM}→ POST {base_url}/v1/messages{RESET}")
    print(f"{DIM}→ Bearer {token[:8]}…  model={args.model}{RESET}")
    print()

    try:
        resp = httpx.post(
            f"{base_url}/v1/messages",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json=payload,
            timeout=30.0,
        )
    except httpx.ConnectError:
        print(f"{RED}✗ Connection refused at {base_url}.{RESET}")
        print(f"  Is Tourniquet running? Start it with: {BOLD}tourniquet start{RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"{RED}✗ Request failed: {e}{RESET}")
        sys.exit(1)

    # Pre-flight blocked
    if resp.status_code == 402:
        body = resp.json().get("error", {})
        kind = body.get("type", "")
        print(f"{YELLOW}🛑 PRE-FLIGHT BLOCKED{RESET} — Tourniquet stopped this before it reached Anthropic.")
        print(f"  Type     : {kind}")
        print(f"  Reason   : {body.get('message', '?')}")
        if "display" in body:
            d = body["display"]
            print(f"  Today    : {d.get('spent', '?')} of {d.get('cap', '?')}")
            if "projected" in d:
                print(f"  Projected: {d.get('projected', '?')} (over by {d.get('overage', d.get('tolerance', '?'))})")
        if body.get("lift_active"):
            print(f"  Lift active until {body.get('lift_expires_at', '?')}")
        print()
        print(f"  {DIM}Lift today: tourniquet lift <key> --multiplier 2{RESET}")
        sys.exit(0)

    if resp.status_code == 401:
        print(f"{RED}✗ 401 — your tq_ token is unknown to this proxy.{RESET}")
        print(f"  Check {BOLD}tourniquet status{RESET} or create a fresh key.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"{RED}✗ HTTP {resp.status_code}{RESET}")
        print(f"  Body: {resp.text[:300]}")
        sys.exit(1)

    # Success — parse Anthropic's response shape
    body = resp.json()
    content = body.get("content", [])
    text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
    usage = body.get("usage", {}) or {}
    in_tokens = int(usage.get("input_tokens", 0) or 0)
    out_tokens = int(usage.get("output_tokens", 0) or 0)
    model = body.get("model", args.model)
    request_id = body.get("id", "")

    # Compute cost via the same logic the proxy uses
    try:
        from tourniquet.billing.formatting import format_money
        from tourniquet.billing.pricing import cost_usd_cents
        from tourniquet.config import settings
        cost_cents = cost_usd_cents(model, in_tokens, out_tokens)
        cost_str = format_money(cost_cents, settings.display_currency)
    except Exception:
        cost_str = f"~{(in_tokens + out_tokens)} tokens"

    bar = "─" * 60
    print(f"{GREEN}✓ Routed through Tourniquet → Anthropic → back to you{RESET}")
    print(bar)
    print(f"  {BOLD}Proxy{RESET}        {base_url}")
    print(f"  {BOLD}Model{RESET}        {model}")
    print(f"  {BOLD}Request ID{RESET}   {request_id}")
    print()
    print(f"  {BOLD}You said{RESET}     {args.message!r}")
    print(f"  {BOLD}Claude said{RESET}  {GREEN}{text!r}{RESET}")
    print()
    print(f"  {BOLD}Tokens{RESET}       {in_tokens} in  /  {out_tokens} out")
    print(f"  {BOLD}Cost{RESET}         {cost_str}  {DIM}(billed against your tq_ key's cap){RESET}")
    print(bar)
    print(f"  {DIM}Open the dashboard to see this request in the live spend bar:{RESET}")
    print(f"  {base_url.replace('/v1/messages', '')}/dashboard")
    print()


def cmd_test_alerts(args: argparse.Namespace) -> None:
    """Fire a synthetic alert through every configured channel and report status.

    Use this to verify your alert setup without burning real tokens. The event
    is clearly labelled [TEST] so anyone seeing it knows it's a smoke check.
    """
    import asyncio
    from datetime import date

    from tourniquet.alerts.notifier import AlertEvent, fan_out
    from tourniquet.config import settings

    # Force-enable desktop for this test only (overrides .env without touching it)
    if args.enable_desktop:
        settings.enable_desktop_notifications = "true"
        settings.enable_mac_notifications = "true"

    threshold_map = {"50": 50, "80": 80, "100": 100, "cap-hit": -1}
    threshold = threshold_map.get(args.threshold, 80)

    # Try to bind to the real key — gives the alert the correct cap and a real
    # key_id so in-app one-tap actually mutates that key's lifted_cap. Falls
    # back to a synthetic event if the named key isn't in the DB.
    real_key = _lookup_key_by_name(args.key)
    if real_key is not None:
        cap_cents = real_key.daily_cap_usd_cents or 100
        api_key_id = str(real_key.id)
        api_key_name = f"[TEST] {real_key.name}"
        bind_note = f"bound to real key '{real_key.name}' (cap ${cap_cents/100:.2f})"
    else:
        cap_cents = 500
        api_key_id = "00000000-0000-0000-0000-000000000000"
        api_key_name = f"[TEST] {args.key}"
        bind_note = f"synthetic (no key named '{args.key}' — taps won't persist)"

    spent_cents = cap_cents if threshold == -1 else int(cap_cents * threshold / 100)

    event = AlertEvent(
        api_key_name=api_key_name,
        threshold_pct=threshold,
        spent_usd_cents=spent_cents,
        cap_usd_cents=cap_cents,
        display_currency=settings.display_currency,
        today=date.today(),
        api_key_id=api_key_id,
        recovery_offer=args.recovery,
    )

    is_tty = sys.stdout.isatty()
    GREEN = "\033[32m" if is_tty else ""
    RED = "\033[31m" if is_tty else ""
    DIM = "\033[2m" if is_tty else ""
    BOLD = "\033[1m" if is_tty else ""
    RESET = "\033[0m" if is_tty else ""

    label = "cap-hit" if threshold == -1 else f"{threshold}%"
    monitor_str = (
        "monitor mode (kill_enabled=False, kill-now URL embedded)"
        if args.monitor
        else "standard (kill_enabled=True)"
    )
    print()
    print(f"🧪 {BOLD}Tourniquet test-alerts{RESET}")
    print(f"   Key:        {api_key_name}")
    print(f"   Binding:    {bind_note}")
    print(f"   Threshold:  {label}")
    print(f"   Mode:       {monitor_str}")
    print()

    results = asyncio.run(fan_out(event, kill_enabled=not args.monitor))

    channel_order = ["jsonl", "desktop", "slack", "telegram", "email", "webhook"]
    width = max(len(c) for c in channel_order)
    print(f"  {BOLD}Channel{RESET}      {BOLD}Status{RESET}")
    for ch in channel_order:
        status = results.get(ch, "skipped:no-config")
        if status == "sent":
            icon, note = GREEN + "✅" + RESET, "delivered"
        elif status.startswith("error:"):
            icon, note = RED + "❌" + RESET, status[6:][:80]
        else:
            icon, note = DIM + "—" + RESET, "not configured"
        print(f"  {icon}  {ch:<{width}}  {note}")
    print()

    skipped = [c for c, v in results.items() if v == "skipped:no-config"]
    if skipped:
        print(f"  {BOLD}To enable skipped channels{RESET} — add to {DIM}~/.tourniquet/.env{RESET}:")
        print()
        if "desktop" in skipped:
            print(f"    {BOLD}desktop{RESET} (Mac/Win/Linux banner notifications)")
            print(f"      ENABLE_MAC_NOTIFICATIONS=true")
            print(f"      MAC_NOTIFICATION_STYLE=both     {DIM}text | action | both{RESET}")
            print()
        if "slack" in skipped:
            print(f"    {BOLD}slack{RESET}")
            print(f"      SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
            print(f"      {DIM}create at https://api.slack.com/apps → Incoming Webhooks{RESET}")
            print()
        if "telegram" in skipped:
            print(f"    {BOLD}telegram{RESET}")
            print(f"      TELEGRAM_BOT_TOKEN=123456:ABC...")
            print(f"      TELEGRAM_CHAT_ID=987654")
            print(f"      {DIM}bot token: chat @BotFather → /newbot{RESET}")
            print(f"      {DIM}chat id:   chat @userinfobot → it replies with your id{RESET}")
            print()
        if "webhook" in skipped:
            print(f"    {BOLD}webhook{RESET} (generic — Zapier/n8n/Home Assistant)")
            print(f"      ALERT_WEBHOOK_URL=https://hooks.zapier.com/...")
            print()
        if "email" in skipped:
            print(f"    {BOLD}email{RESET} (Resend)")
            print(f"      RESEND_API_KEY=re_...")
            print(f"      RESEND_FROM_EMAIL=alerts@yourdomain.com")
            print(f"      {DIM}free tier at resend.com — domain must be verified{RESET}")
            print(f"      {DIM}per-key alert_email also needs setting in the dashboard{RESET}")
            print()
        print(f"  Then run {BOLD}tourniquet test-alerts{RESET} again.")
        print()
    else:
        print(f"  {GREEN}All configured channels delivered.{RESET}")
        print()


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

    # test — pretty smoke test
    p_test = sub.add_parser("test", help="Send a test request through the proxy and pretty-print")
    p_test.add_argument("--token", help="tq_ token (default: $ANTHROPIC_API_KEY)")
    p_test.add_argument("--base-url", dest="base_url", help="Proxy URL (default: $ANTHROPIC_BASE_URL or 127.0.0.1:8787)")
    p_test.add_argument("--message", default="say hi in 5 words", help="Prompt content")
    p_test.add_argument("--model", default="claude-haiku-4-5-20251001", help="Model ID")

    # test-alerts — fire synthetic alert through all configured channels
    p_test_alerts = sub.add_parser(
        "test-alerts",
        help="Fire a synthetic alert through every configured channel",
    )
    p_test_alerts.add_argument(
        "--threshold",
        choices=["50", "80", "100", "cap-hit"],
        default="80",
        help="Threshold to simulate (default: 80)",
    )
    p_test_alerts.add_argument(
        "--key",
        default="ojw-swarm",
        help="Key name to embed in the message (default: ojw-swarm)",
    )
    p_test_alerts.add_argument(
        "--monitor",
        action="store_true",
        help="Simulate monitor mode (kill_enabled=False) — adds kill-now URL",
    )
    p_test_alerts.add_argument(
        "--enable-desktop",
        action="store_true",
        help="Force-enable desktop notifications for this test only",
    )
    p_test_alerts.add_argument(
        "--recovery",
        action="store_true",
        help="Send a recovery-offer alert (post-kill 'want to bump?' prompt with +$N buttons)",
    )

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
        "test": cmd_test,
        "test-alerts": cmd_test_alerts,
        "register-url-handler": cmd_register_url_handler,
        "handle-url": cmd_handle_url,
    }
    dispatch[args.subcommand](args)
