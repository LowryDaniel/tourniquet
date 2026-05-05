from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    fernet_key: str
    secret_key: str

    resend_api_key: str = ""
    resend_from_email: str = "alerts@burnrate.ai"

    sentry_dsn: str = ""
    app_env: str = "development"
    log_level: str = "INFO"
    app_base_url: str = "http://localhost:8000"

    anthropic_base_url: str = "https://api.anthropic.com"

    magic_link_expiry_seconds: int = 900  # 15 minutes


settings = Settings()  # type: ignore[call-arg]
