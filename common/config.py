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
    # Decision gate (analyst/decide.py): gemini-decision-1 skips the tool loop for candidate
    # clusters where a web check cannot change the call. Those get a Nemotron verdict (Verda),
    # escalated to GEMINI_MODEL single-shot when it disagrees with decision-1's own call.
    gemini_decision_model: str = "gemini-decision-1"  # "" turns the gate off
    analyst_gate_max_p_web: float = 0.1  # skip tools when P(web check matters) is below this
    analyst_fast_nemotron: bool = True  # needs VERDA_INFERENCE_KEY; off = Hydrogen single-shot
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
    # Screenshot read (media/screenshot.py): a narrow extraction, reasoning off is about 3x faster
    media_screenshot_reasoning: bool = False
    media_screenshot_max_tokens: int = 1536

    oxylabs_web_api_key: str = ""
    context_dev_api_key: str = ""

    # Link lookup (lookup/README.md): every lookup spends live Oxylabs, Nemotron and Gemini calls.
    lookup_max_per_hour: int = 60  # all clients together; 0 = no cap
    lookup_max_per_ip_per_hour: int = 10
    lookup_cache_ttl_s: float = 1800  # same input within this window returns the earlier result
    lookup_job_ttl_s: float = 3600
    lookup_verify_max: int = 8  # candidate X copies whose own page is read
    lookup_profiles_max: int = 4  # copier profiles read (account age, followers)
    # cap (s) per read of a candidate copy or older page; slower ones stay unread mentions
    lookup_verify_read_s: float = 8
    lookup_time_budget_s: float = 60  # searches + page reads; the last 10 s are the reading's
    lookup_media: bool = True  # Nemotron reads the post's images
    # The cited reading (lookup/assess.py, lookup/README.md has the measurement): Nemotron 3.5
    # Super VL writes it (~1.5 s, reasoning off: reasoning on took 7-15 s and once ran out of
    # tokens); Gemini adds a slower second reading on the same stream (~15 s), shown beside it.
    lookup_nemotron_reading: bool = True
    lookup_reading_reasoning: bool = False
    lookup_reading_max_tokens: int = 2048
    lookup_reading_timeout_s: float = 15
    lookup_gemini: bool = True  # Gemini's second reading
    # its own bound; it never delays the first reading. Gemini answers 504 when a reading needs
    # longer than the timeout it is sent (#60), and big readings pass 30 s.
    lookup_second_reading_s: float = 45
    lookup_provider_cache_ttl_s: float = 600  # same Oxylabs search/page within 10 min: cached

    jetstream_url: str = "wss://jetstream2.us-east.bsky.network/subscribe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
