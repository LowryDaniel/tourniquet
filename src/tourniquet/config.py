import os
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file_candidates() -> list[str]:
    """Priority order for .env discovery.

    Pydantic-settings merges all found files and uses the last-listed value
    when the same key appears in multiple files (last write wins).

    We want CWD/.env to override ~/.tourniquet/.env, which overrides the
    TOURNIQUET_CONFIG_DIR path, so we list them in ascending priority order
    (last = highest priority). This lets a local dev .env shadow the system-wide
    config for quick testing.
    """
    candidates: list[str] = []
    override = os.environ.get("TOURNIQUET_CONFIG_DIR")
    if override:
        candidates.append(str(Path(override) / ".env"))
    candidates.append(str(Path.home() / ".tourniquet" / ".env"))
    candidates.append(".env")  # CWD — listed last so its values win
    return candidates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
    )

    database_url: str
    fernet_key: str
    secret_key: str

    @field_validator("fernet_key")
    @classmethod
    def _validate_fernet(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as e:
            raise ValueError(f"FERNET_KEY invalid (must be 32 url-safe base64 bytes): {e}") from e
        return v

    @field_validator("secret_key")
    @classmethod
    def _validate_secret(cls, v: str) -> str:
        if len(v.encode()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 bytes")
        return v

    resend_api_key: str = ""
    resend_from_email: str = "alerts@tourniquet.ai"

    sentry_dsn: str = ""
    app_env: str = "development"
    log_level: str = "INFO"
    app_base_url: str = "http://localhost:8000"

    anthropic_base_url: str = "https://api.anthropic.com"

    # ── Proxy request hardening ───────────────────────────────────────────────
    # Hard ceiling on POST /v1/messages bodies. Reading unbounded bytes into
    # memory from `await request.body()` is reachable on Tailscale / cloud-VM
    # deployments; a 1GB malicious payload would OOM the process. 10 MiB is
    # generous for legitimate prompts (Anthropic's own request-size guidance
    # caps inputs well below this) while giving a deterministic ceiling.
    max_request_body_bytes: int = 10_485_760  # 10 MiB

    magic_link_expiry_seconds: int = 900  # 15 minutes

    display_currency: str = "USD"  # env var: DISPLAY_CURRENCY

    absolute_ceiling_usd_cents: int = 10000   # env: ABSOLUTE_CEILING_USD_CENTS
    suggestion_window_days: int = 14          # env: SUGGESTION_WINDOW_DAYS

    # ── Pre-flight max-cost guard ─────────────────────────────────────────────
    # Before proxying a request to Anthropic, estimate its worst-case cost and
    # reject pre-flight with HTTP 402 if it would exceed your cap by more than
    # BOTH (max_overage_abs_cents) AND (max_overage_pct%). Small overages are
    # allowed (let it ride) — this is why we use AND, not OR. Protects against
    # runaway requests while being lenient on small overages.
    max_overage_abs_cents: int = 50      # env: MAX_OVERAGE_ABS_CENTS — 50¢
    max_overage_pct: int = 10            # env: MAX_OVERAGE_PCT — 10% of cap

    # ── Alert channels ────────────────────────────────────────────────────────
    slack_webhook_url: str = ""
    # Optional Socket Mode app-level token (xapp-...). When set, Tourniquet
    # opens a WebSocket to Slack so inline button taps apply in-app — no
    # public HTTPS callback URL needed. Setup steps in docs/alerts-setup.md.
    slack_app_token: str = ""
    # Bot User OAuth Token (xoxb-...) and channel/DM ID — required alongside
    # slack_app_token to enable in-app one-tap via Block Kit + Socket Mode.
    # Without these, Slack stays on the webhook + mrkdwn-link fallback.
    slack_bot_token: str = ""
    slack_channel_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_webhook_url: str = ""
    # set ENABLE_MAC_NOTIFICATIONS=true to enable (macOS only, kept for compat)
    enable_mac_notifications: bool = False
    # set ENABLE_DESKTOP_NOTIFICATIONS=true to enable on all platforms
    enable_desktop_notifications: bool = False
    mac_notification_style: str = "both"           # "text" | "action" | "both"
    # X-Telegram-Bot-Api-Secret-Token value, set when configuring the bot
    telegram_webhook_secret: str = ""


settings = Settings()
