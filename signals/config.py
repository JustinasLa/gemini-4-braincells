"""All detector thresholds in one versioned place. Bump VERSION whenever a value changes, so every
stored cluster says which settings produced it (clusters.detector_version)."""

from dataclasses import asdict, dataclass, field

VERSION = "v1.2"


@dataclass(frozen=True)
class DetectorConfig:
    version: str = VERSION
    # Windows: name -> length in seconds.
    windows: dict[str, int] = field(default_factory=lambda: {"10m": 600, "1h": 3600})

    # --- keys (shared things accounts can do) ---
    min_text_tokens: int = 5  # shorter texts ("LOL", "thanks") collide organically
    min_text_chars: int = 25
    minhash_min_tokens: int = 8  # near-duplicate matching only for longer texts
    minhash_threshold: float = 0.6  # Jaccard over word 3-shingles
    minhash_perm: int = 64
    max_key_accounts: int = 150  # keys shared by more accounts are "popular": no edges
    min_key_accounts: int = 2

    # --- edges / clustering ---
    min_engagement_shared: int = 3  # distinct co-engaged subjects (likes/reposts/quotes/follows)
    min_cluster_size: int = 4
    max_cluster_size: int = 500
    leiden_above: int = 50  # components bigger than this are split into communities
    leiden_resolution: float = 1.0
    seed: int = 1729

    # --- signals ---
    sync_seconds: float = 60.0  # burst synchrony window
    min_burst_actions: int = 4
    regularity_min_events: int = 6
    regularity_max_cv: float = 0.15  # inter-event coefficient of variation: below = clockwork
    fresh_account_days: int = 30
    family_fire: float = 0.5  # a family "fires" when its score reaches this
    permutations: int = 500

    # --- status ---
    min_families_coordinated: int = 2
    max_p_coordinated: float = 0.01
    min_score_coordinated: float = 0.6
    organic_guard_strong: float = 0.6

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT = DetectorConfig()
