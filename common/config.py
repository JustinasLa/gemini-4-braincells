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
    nvidia_media_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    # Dedicated Nemotron 3.5 Super VL endpoint (vLLM on Verda); media/ prefers it when keyed.
    verda_inference_key: str = ""
    verda_base_url: str = "https://containers.datacrunch.io/nemotron-3-5-super-vl-b300/v1"
    verda_model: str = "nemotron-3.5-super-vl"
    # media/ worker (Nemotron reads images in candidate clusters; media/README.md)
    media_statuses: str = "coordinated,insufficient"  # detector statuses to read, priority order
    media_min_score: float = 0.5
    media_max_per_cluster: int = 4  # unique media per cluster, most reused first
    media_rpm: float = 30
    media_max_tokens: int = 4096  # reasoning tokens count against this; too small => null content
    media_reasoning: bool = True

    oxylabs_web_api_key: str = ""
    context_dev_api_key: str = ""

    # Link lookup (lookup/README.md): every lookup spends live Oxylabs, Nemotron and Gemini calls.
    lookup_max_per_hour: int = 60  # all clients together; 0 = no cap
    lookup_max_per_ip_per_hour: int = 10
    lookup_cache_ttl_s: float = 1800  # same input within this window returns the earlier result
    lookup_job_ttl_s: float = 3600
    lookup_verify_max: int = 8  # candidate X copies whose own page is read
    lookup_profiles_max: int = 4  # copier profiles read (account age, followers)
    lookup_time_budget_s: float = 150
    lookup_media: bool = True  # Nemotron reads the post's images
    lookup_gemini: bool = True  # Gemini writes the cited reading

    jetstream_url: str = "wss://jetstream2.us-east.bsky.network/subscribe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
