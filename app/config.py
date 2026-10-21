from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "change-me-telegram-secret"

    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_verify_token: str = "change-me-strava-verify-token"
    strava_redirect_uri: str = "http://localhost:8000/oauth/strava/callback"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    # Comma-separated extra models tried after primary/fallback on 503/404
    gemini_model_cascade: str = (
        "gemini-3.5-flash-lite,gemini-3-flash-preview,gemini-flash-lite-latest"
    )

    public_base_url: str = "http://localhost:8000"
    redis_url: str = "redis://localhost:6379/0"
    confidence_threshold: float = 0.75

    telegram_polling: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
