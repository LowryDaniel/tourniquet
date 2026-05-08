import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file_candidates() -> list[str]:
    """Priority order for .env discovery (pydantic-settings merges all found
    files, last-listed wins for duplicate keys).

    We want:  CWD/.env > ~/.tourniquet/.env > $TOURNIQUET_CONFIG_DIR/.env
    So list them in ascending priority (last = highest priority).
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

    resend_api_key: str = ""
    resend_from_email: str = "alerts@tourniquet.ai"

    sentry_dsn: str = ""
    app_env: str = "development"
    log_level: str = "INFO"
    app_base_url: str = "http://localhost:8000"

    anthropic_base_url: str = "https://api.anthropic.com"

    magic_link_expiry_seconds: int = 900  # 15 minutes

    display_currency: str = "USD"  # env var: DISPLAY_CURRENCY

    absolute_ceiling_usd_cents: int = 10000   # env: ABSOLUTE_CEILING_USD_CENTS
    suggestion_window_days: int = 14          # env: SUGGESTION_WINDOW_DAYS

    # ── Pre-flight max-cost guard ─────────────────────────────────────────────
    # When a single request's worst-case cost would push you over today's cap by
    # more than (max_overage_abs_cents) AND more than (max_overage_pct%), block
    # the request pre-flight with 402. Small overages are allowed (let it ride).
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
    enable_mac_notifications: str = "false"        # "true" to enable (macOS only, kept for compat)
    enable_desktop_notifications: str = ""         # "true" to enable on all platforms
    mac_notification_style: str = "both"           # "text" | "action" | "both"
    telegram_webhook_secret: str = ""        # X-Telegram-Bot-Api-Secret-Token value, set when configuring the bot


settings = Settings()  # type: ignore[call-arg]
