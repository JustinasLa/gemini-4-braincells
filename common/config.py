"""Settings read from the environment (and .env). Names match .env.example."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://noise:change-me@localhost:5432/noise"

    gemini_api_key: str = ""
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    oxylabs_username: str = ""
    oxylabs_password: str = ""
    context_dev_api_key: str = ""

    jetstream_url: str = "wss://jetstream2.us-east.bsky.network/subscribe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
