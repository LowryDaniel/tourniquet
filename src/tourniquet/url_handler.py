"""Register the tourniquet:// URL scheme as a system handler.

Supported platforms:
  win32  — HKCU\\Software\\Classes\\tourniquet registry keys (winreg)
  linux  — ~/.local/share/applications/tourniquet-url-handler.desktop
  darwin — prints manual Automator / Shortcuts instructions (no .app bundle required)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


# ── Platform implementations ───────────────────────────────────────────────────

def register_windows() -> None:
    """Write HKCU registry keys so Windows dispatches tourniquet:// URLs."""
    try:
        import winreg  # type: ignore[import]
    except ImportError:
        print("ERROR: winreg module unavailable — are you running on Windows?", flush=True)
        return

    base = r"Software\Classes\tourniquet"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Tourniquet Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "open")

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open") as _:
        pass

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, 'tourniquet handle-url "%1"')

    print("Registered tourniquet:// URL handler in HKCU\\Software\\Classes\\tourniquet")


def register_linux() -> None:
    """Write a .desktop file and register it as the tourniquet:// handler."""
    apps_dir = Path.home() / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    desktop_content = (
        "[Desktop Entry]\n"
        "Name=Tourniquet URL Handler\n"
        "Exec=tourniquet handle-url %u\n"
        "Type=Application\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "MimeType=x-scheme-handler/tourniquet;\n"
    )
    desktop_file = apps_dir / "tourniquet-url-handler.desktop"
    desktop_file.write_text(desktop_content, encoding="utf-8")
    desktop_file.chmod(0o755)

    for cmd in [
        ["update-desktop-database", str(apps_dir)],
        ["xdg-mime", "default", "tourniquet-url-handler.desktop", "x-scheme-handler/tourniquet"],
    ]:
        try:
            subprocess.run(cmd, check=False, capture_output=True)
        except FileNotFoundError:
            pass  # command not available — skip gracefully

    print(f"Registered tourniquet:// URL handler: {desktop_file}")


def register_macos() -> None:
    """Print setup instructions — real macOS registration requires an .app bundle."""
    print(
        "macOS registration requires an .app bundle, which is out of scope for the CLI.\n"
        "\n"
        "Option A — Shortcuts.app:\n"
        "  1. Open Shortcuts.app → File → New Shortcut\n"
        "  2. Add a 'Run Shell Script' action with body:\n"
        "       tourniquet handle-url \"$1\"\n"
        "  3. In Shortcuts preferences, enable 'Allow Running Scripts'\n"
        "\n"
        "Option B — Automator:\n"
        "  1. Open Automator → New Document → Application\n"
        "  2. Add 'Run Shell Script' action:\n"
        "       tourniquet handle-url \"$1\"\n"
        "  3. Save as tourniquet-handler.app to ~/Applications\n"
        "  4. Run once to register the bundle; then run:\n"
        "       /System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
        " -f ~/Applications/tourniquet-handler.app\n"
    )


def register() -> None:
    """Dispatch to the platform-specific registration function."""
    if sys.platform == "win32":
        register_windows()
    elif sys.platform == "darwin":
        register_macos()
    else:
        register_linux()


# ── URL parsing / dispatch ─────────────────────────────────────────────────────

def handle_url(url: str) -> int:
    """Parse a tourniquet:// URL and dispatch the appropriate action.

    Supports:
      tourniquet://lift/<key_id>
      tourniquet://lift/<key_id>?multiplier=2

    Returns 0 on success, non-zero on error.
    """
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if parsed.scheme != "tourniquet":
        print(f"ERROR: unsupported scheme {parsed.scheme!r} — expected 'tourniquet'", flush=True)
        return 1

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    host = parsed.netloc  # e.g. "lift"

    if not path_parts and not host:
        print("ERROR: empty URL path", flush=True)
        return 1

    # Support both tourniquet://lift/<id> and tourniquet:///lift/<id>
    action = host or path_parts[0]
    rest = path_parts if host else path_parts[1:]

    if action == "lift":
        if not rest:
            print("ERROR: missing key_id in URL", flush=True)
            return 1

        key_id = rest[0]
        qs = parse_qs(parsed.query)
        multiplier = float(qs.get("multiplier", ["2.0"])[0])
        return _do_lift(key_id, multiplier)

    print(f"ERROR: unknown action {action!r}", flush=True)
    return 1


def _do_lift(key_id: str, multiplier: float) -> int:
    """Lift the cap for key_id using the same logic as `tourniquet lift`."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from tourniquet.billing.formatting import format_money
    from tourniquet.config import settings
    from tourniquet.db import get_session
    from tourniquet.models import ApiKey

    async def _run() -> int:
        async with get_session() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.id == key_id)  # type: ignore[arg-type]
            )
            key = result.scalar_one_or_none()
            if not key:
                print(f"ERROR: no key with id {key_id!r}", flush=True)
                return 1

            now = datetime.now(timezone.utc)
            tomorrow = now.date() + timedelta(days=1)
            expires_at = datetime(
                tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc
            )
            raw = int(key.daily_cap_usd_cents * multiplier)
            lifted = min(raw, key.absolute_ceiling_usd_cents)
            key.lifted_cap_usd_cents = lifted
            key.lift_expires_at = expires_at
            await session.commit()

            lifted_display = format_money(lifted, settings.display_currency)
            print(f"Cap lifted to {lifted_display} until midnight UTC.")
        return 0

    return asyncio.run(_run())
