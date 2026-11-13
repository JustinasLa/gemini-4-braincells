"""Settings read from the environment (and .env). Names match .env.example."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://noise:change-me@localhost:5432/noise"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-4-hydrogen"  # verdict model (analyst/README.md has the comparison)
    gemini_batch_model: str = "gemini-3.5-flash-lite"  # cheap model for bulk, no-tool verdicts
    # Analyst function calling (analyst/agent.py): Gemini may call Oxylabs + DB lookups.
    analyst_tools: bool = True
    analyst_max_tool_calls: int = 6
    analyst_max_web_calls: int = 4  # live Oxylabs requests per cluster (cache hits are free)
    analyst_time_budget_s: float = 120.0
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    oxylabs_web_api_key: str = ""
    context_dev_api_key: str = ""

    jetstream_url: str = "wss://jetstream2.us-east.bsky.network/subscribe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
