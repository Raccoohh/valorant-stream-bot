"""Centralized application configuration via Pydantic Settings.

Every tunable value in the system lives here. Settings are read from
environment variables (case-insensitive) and from a local .env file,
so the same code runs identically on your laptop and inside Docker.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Infrastructure ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/valorant_stream"
    redis_url: str = "redis://localhost:6379/0"

    # --- Twitch ---
    twitch_token: str = Field(default="", description="OAuth token for the bot account")
    twitch_channel: str = "raccoohh"
    twitch_prefix: str = "!"
    # App credentials (dev.twitch.tv console) — used by the stream watcher
    # to poll Get Streams with a client-credentials app access token.
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_poll_interval_seconds: int = 60

    # --- HenrikDev Valorant API ---
    henrik_api_key: str = ""
    henrik_base_url: str = "https://api.henrikdev.xyz"
    valorant_name: str = "raccoohh"
    valorant_tag: str = "EUW"
    valorant_region: str = "eu"
    valorant_platform: str = "pc"  # v4 requires an explicit platform segment
    henrik_mode: str = ""  # optional queue filter, e.g. "competitive"; empty = all

    # --- Rate limiting (HenrikDev 429 handling) ---
    henrik_max_retries: int = 5          # per-cycle retries before giving up the cycle
    henrik_retry_base_seconds: float = 5.0   # first backoff delay
    henrik_retry_max_seconds: float = 120.0  # backoff cap

    # --- Poller ---
    poll_interval_seconds: int = 60

    # --- Server ---
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # --- Dev tooling ---
    # Mounts the /dev event simulator. MUST be false in any real deployment.
    enable_dev_routes: bool = True

    # --- Event bus ---
    redis_event_channel: str = "valorant:events"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor — import this everywhere instead of instantiating Settings directly."""
    return Settings()
