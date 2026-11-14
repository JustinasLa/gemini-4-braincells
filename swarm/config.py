"""All meme swarm thresholds in one versioned place. Bump VERSION whenever a value changes, so every
stored report says which settings produced it (SwarmReport.version)."""

from dataclasses import asdict, dataclass

VERSION = "swarm-v3"


@dataclass(frozen=True)
class SwarmConfig:
    version: str = VERSION
    seed: int = 1729

    # --- verdict ---
    min_accounts: int = 5  # fewer distinct posting accounts: always "unclear"
    family_fire: float = 0.5  # a family "fires" at this score
    min_families_swarm: int = 2
    min_score_swarm: float = 0.6
    max_p_swarm: float = 0.01  # timing p needed when timing is one of only two families fired
    guard_strong: float = 0.6
    # without an observation window, timed posts spanning less than this cannot show an organic
    # spread either (a search's newest posts): no "organic" verdict then
    organic_min_span_s: float = 1200.0
    # share-based scores are the Wilson lower bound at this z (95%):
    # "5 of 5" ~0.57, "43 of 43" ~0.92
    wilson_z: float = 1.96
    timeline_max: int = 300
    max_examples: int = 12

    # --- timing ---
    permutations: int = 500  # null worlds per test (cap)
    early_stop_after: int = 100  # stop a test after this many worlds when p is clearly large
    early_stop_p: float = 0.1
    p_strong: float = 0.01  # a tested signal counts in full at or below this (adjusted) p
    p_weak: float = 0.05  # ... for half (timing) or capped (accounts) at or below this
    burst_windows_s: tuple[int, ...] = (60, 300, 1200)  # tested burst widths
    # null worlds keep how many accounts posted in each bin of width factor x window and place
    # them at random inside it ("interval jitter"): a slow natural rise is kept, a spike is not.
    # Bins are clipped to [first post, end of the observation window] (or the last post).
    burst_jitter_factor: float = 12.0
    min_burst_accounts: int = 5  # timed accounts needed to run the tests
    same_second_s: float = 3.0
    same_second_jitter_s: float = 120.0
    min_same_second_accounts: int = 3
    coarse_clock_share: float = 0.8  # this share of times on whole minutes: no seconds-level tests
    # clockwork is a threshold, not a test: a run of near-equal gaps between different accounts
    clockwork_min_posts: int = 8
    clockwork_min_accounts: int = 4
    clockwork_tol: float = 0.15  # each gap within 15% (+ clockwork_tol_s) of the run's first gap
    clockwork_tol_s: float = 1.0
    clockwork_min_gap_s: float = 10.0  # shorter gaps are same_second territory
    clockwork_full_gaps: int = 10

    # --- accounts ---
    min_known: int = 5  # accounts that must have a field before a signal shows
    # a baseline-tested signal (fresh accounts, empty profiles) fires only with p <= p_strong and
    # at least this many accounts with the field; otherwise it is capped below family_fire
    min_tested_accounts: int = 8
    no_baseline_cap: float = 0.45  # untested account shares (no baseline) never fire a family
    # A tested share scores its excess over the base rate: the Wilson bound rescaled so the base
    # rate scores 0 and a share this far from the base rate towards 100% scores 1 (against a 3%
    # base rate: a bound of 0.39 fires, 0.76 is as strong as the signal gets). Also used for
    # minted handles and copied bios. 0.5 made half-new communities with a copied caption a
    # swarm in 62% of synthetic runs; 0.75 in 12%, and only when 60%+ of them were new.
    excess_full: float = 0.75
    baseline_min_n: int = 100  # baseline sample needed for the binomial tests
    fresh_account_days: int = 30
    # "joined the same week" (summary wording only) counts accounts created this recently before
    # the group's first post: an old signup wave is ordinary
    cohort_recent_days: int = 60
    cohort_days: int = 7
    min_cohort_accounts: int = 4
    handle_background: float = 0.25  # share of ordinary handles that share a shape anyway
    min_template_handles: int = 6  # accounts with a handle before templated_handles runs
    min_template_accounts: int = 4
    # minted handles: one stem plus a counter, the way a ring names its accounts. Either a stem of
    # 5+ letters with only a number after it (goodstuff01, goodstuff-2, nextbuy), or a short tag
    # of 2-4 letters after a name, right before the number (logannb001, jodygood02). A family
    # needs min_template_accounts accounts with distinct suffixes, at least min_template_accounts
    # and half of them small counters (<= minted_counter_max, or none); fans pick meaningful
    # numbers (13, 1989)
    minted_stem_min_letters: int = 5
    minted_tag_letters: tuple[int, int] = (2, 4)
    minted_tag_min_name: int = 3
    minted_counter_max: int = 20
    minted_background: float = 0.05  # share of ordinary handles that look minted anyway
    bio_min_words: int = 3
    bio_full_words: int = 6
    name_min_chars: int = 6
    name_weight: float = 0.7
    min_same_bio_accounts: int = 3
    # share of bios (or names) that are identical anyway: the share is measured among accounts
    # that have one, and scored as its excess over this
    same_bio_background: float = 0.02
    established_days: int = 365
    established_followers: int = 100

    # --- copy ---
    # weight of reused words: (words / text_full_words) ** 2, capped at 1 ("lol" ~ 0.016)
    text_full_words: int = 8
    min_copy_accounts: int = 4  # full weight from this many reusing accounts
    near_min_words: int = 6
    near_jaccard: float = 0.6  # over word 3-shingles (signals/textnorm.py)
    near_max_texts: int = 200
    near_weight: float = 0.9
    own_words_min_words: int = 3
    min_text_coverage: float = 0.3  # own_words counts in full when this share wrote something

    # --- boost ---
    min_boosts: int = 5  # reposts, quotes and replies of the posts (likes never count for a loop)
    min_loop_accounts: int = 3
    fast_boost_s: float = 120.0
    fast_boost_min_targets: int = 3  # a booster that fast-boosts this many different posts
    min_fast_boosters: int = 3  # ... and at least this many such boosters
    fast_boost_min_per_post: int = 2  # a post counts when this many of them fast-boosted it
    fast_boost_full_posts: int = 5
    repeat_min_posts: int = 3
    min_repeat_accounts: int = 3
    boost_ratio_min_accounts: int = 5
    boost_ratio_min_reposts: int = 5
    boost_ratio_weight: float = 0.6  # weak: only fires when nearly every account looks like it

    # --- guards ---
    slow_spread_h: float = 6.0
    slow_spread_full_h: float = 24.0
    # below guard_strong on purpose: slow drip campaigns exist, so a slow spread alone never
    # vetoes a swarm or makes a picture organic; it only adds to other guards in the report
    slow_spread_max: float = 0.5
    # below guard_strong too: bots re-upload, and some platforms give every upload a new key
    own_files_max: float = 0.5
    min_conversing_accounts: int = 2
    # one_publisher: the posting accounts openly carry one name (a leisure trust's venue accounts,
    # a franchise's branches). The only guard that overrides fired families, and only when the
    # accounts family did not fire: see score.py. A stem of 5+ letters in the handle or display
    # name, held by min share of the posting accounts, where the rest of each name is words (2+
    # letters), not a counter, and most holders were created within publisher_created_days.
    publisher_stem_min: int = 5
    publisher_min_accounts: int = 4
    publisher_min_share: float = 0.6
    publisher_created_days: int = 31
    publisher_together_share: float = 0.6  # of the holders with a known creation date
    publisher_known_share: float = 0.67  # posting accounts with a known age for "long-standing"
    publisher_established_days: int = 365  # median account age for "organic" (else unclear)
    # tokens that say nothing about who publishes: never a stem, nor what is left of one
    publisher_generic: tuple[str, ...] = (
        "bsky",
        "social",
        "official",
        "news",
        "the",
        "real",
        "team",
        "account",
        "media",
        "online",
        "info",
        "live",
        "daily",
        "world",
        "global",
        "channel",
        "radio",
        "updates",
        "update",
        "store",
        "shop",
        "club",
        "fans",
        "army",
        "stan",
        "music",
        "photo",
        "photos",
        "official",
        "group",
        "company",
        "network",
        "studio",
        "studios",
        "digital",
        "community",
        "page",
    )

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT = SwarmConfig()
