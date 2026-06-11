"""First-run setup for Tourniquet.

Run this ONCE on a fresh install:

    python scripts/init.py

It will:
  1. Create `.env` from `.env.example` if missing.
  2. Generate fresh FERNET_KEY and SECRET_KEY (cryptographically random) and write them in.
  3. Create the local SQLite database with full schema.
  4. Create `~/.tourniquet/` directory for the alert log.

Idempotent: safe to re-run. Existing keys are NEVER overwritten — re-running just fills
in any blanks.
"""

from __future__ import annotations

import base64
import secrets
import sys
from pathlib import Path

# Make `tourniquet` importable when run from project root
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))


ENV_PATH = _ROOT / ".env"
ENV_EXAMPLE_PATH = _ROOT / ".env.example"
TOURNIQUET_HOME = Path.home() / ".tourniquet"


def _generate_fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _generate_secret_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        if not ENV_EXAMPLE_PATH.exists():
            print(f"ERROR: neither .env nor .env.example exists at {_ROOT}", file=sys.stderr)
            sys.exit(1)
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text())
        print(f"  ✓ Created {ENV_PATH} from .env.example")
    return ENV_PATH.read_text().splitlines(keepends=False)


def _patch_env_value(lines: list[str], key: str, generator) -> tuple[list[str], bool]:
    """Replace `KEY=` (empty value) with `KEY=<generated>`. Returns (new_lines, did_change)."""
    out = []
    changed = False
    for line in lines:
        stripped = line.strip()
        # Skip comments and unrelated lines
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key and not v.strip():
            new_value = generator()
            out.append(f"{key}={new_value}")
            changed = True
        else:
            out.append(line)
    return out, changed


def _create_schema() -> None:
    """Run alembic upgrade head against the configured DB."""
    # Import lazily — settings won't load until .env has FERNET_KEY/SECRET_KEY
    from tourniquet.config import settings
    from tourniquet.migrate import upgrade_to_head

    upgrade_to_head(settings.database_url)


def main() -> None:
    print()
    print("=" * 72)
    print("  Tourniquet — first-run setup")
    print("=" * 72)
    print(f"  Project root: {_ROOT}")
    print()

    # Step 1 + 2: ensure .env exists and has crypto keys filled in
    lines = _read_env_lines()

    lines, fernet_changed = _patch_env_value(lines, "FERNET_KEY", _generate_fernet_key)
    if fernet_changed:
        print("  ✓ Generated fresh FERNET_KEY (encrypts your sk-ant- keys at rest)")

    lines, secret_changed = _patch_env_value(lines, "SECRET_KEY", _generate_secret_key)
    if secret_changed:
        print("  ✓ Generated fresh SECRET_KEY (signs session cookies)")

    if fernet_changed or secret_changed:
        ENV_PATH.write_text("\n".join(lines) + "\n")
        print(f"  ✓ Wrote keys to {ENV_PATH}")

    if not (fernet_changed or secret_changed):
        print("  ✓ FERNET_KEY and SECRET_KEY already present — left untouched")

    # Step 3: SQLite schema
    print()
    print("  Creating local database schema...")
    try:
        _create_schema()
        print("  ✓ Schema ready")
    except Exception as exc:
        print(f"  ✗ Schema creation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 4: ~/.tourniquet/ directory for alert log
    TOURNIQUET_HOME.mkdir(parents=True, exist_ok=True)
    print(f"  ✓ Alert log directory: {TOURNIQUET_HOME}")

    print()
    print("=" * 72)
    print("  Setup complete. Next steps:")
    print()
    print("  1. Add your first key:")
    print("       export ANTHROPIC_API_KEY=sk-ant-...")
    print("       python scripts/bootstrap_local.py --email you@example.com \\")
    print("           --name claude-local --cap 5.00")
    print()
    print("  2. Start the proxy:")
    print("       python -m uvicorn tourniquet.main:app --host 127.0.0.1 --port 8787")
    print()
    print("  3. Open the dashboard:")
    print("       http://127.0.0.1:8787/dashboard")
    print()
    print("=" * 72)


if __name__ == "__main__":
    main()
