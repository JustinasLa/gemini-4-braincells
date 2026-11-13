# signals/: coordination detector

Finds groups of Bluesky accounts that act together, scores how strongly, and says why, or
abstains. LLMs are not involved in the decision: `analyst/` explains the status this produces.

```bash
uv run python -m signals run                         # worker: 10m window every minute, 1h every 5 min
uv run python -m signals once --window 10m --dry-run # one pass, print stats, write nothing
uv run pytest tests/test_signals.py
```

## Pipeline

1. **Window** (`window.py`): posts, and the likes/reposts/follows whose subject was hit by
   2..`max_key_accounts` distinct accounts in the window, plus per-second background counts of all
   events (for the null model). Uses `seen_at` (server time).
2. **Keys** (`keys.py`): things accounts share. Content: same normalised text (>= 5 tokens and 25
   chars), MinHash near-duplicate text, same media blob CID, same normalised link. Engagement: same
   liked / reposted / quoted post, same followed account. Keys used by more than
   `max_key_accounts` accounts (viral posts, big news links) are dropped: they can't tell
   coordination from popularity.
3. **Graph** (`graph.py`): account-account edges weighted by rarity, `1/ln(1+n)` per shared key.
   An edge needs one shared content key or >= 3 shared engagement keys. Connected components;
   components over 50 accounts are split with seeded Leiden.
4. **Score** (`score.py`): four families, each the max of its signals (correlated signals in a
   family are never added):

   | Family | Signals |
   |---|---|
   | content | `duplicate_text`, `near_duplicate_text`, `duplicate_media`, `shared_link` (share of members x exclusivity) |
   | timing | `burst_synchrony` (densest 60 s per shared item, with permutation p-value), `clockwork_posting` |
   | network | `repeated_co_engagement`, `reply_flood`, `mutual_amplification` |
   | identity | `fresh_accounts` (PLC creation date), `empty_profiles`, `templated_handles`, `shared_small_pds` |

   Organic guards produce counter-evidence (`polarity: "against"`): `established_accounts`,
   `part_of_wider_crowd`, `language_diversity`, `transparent_automation` (accounts openly share one
   custom handle domain), `clock_aligned_posting` (top-of-the-hour schedulers).
5. **Null model**: each member's whole activity is shifted by its own random offset (circular in
   the window), which keeps every account's own rhythm and breaks cross-account alignment. p-value =
   share of 500 shifts that look at least as synchronized (seeded: same input, same p).
6. **Status** (deterministic, thresholds in `config.py`, version in `clusters.detector_version`):
   - `coordinated`: >= 2 families score >= 0.5, p <= 0.01, score >= 0.6, organic guard < 0.6
   - `likely_organic`: organic guard >= 0.6 and at most one family fired
   - `insufficient`: everything else (we abstain)

   `score` = noisy-OR of the two strongest family scores. `confidence` is not yet calibrated:
   `min(score, 1 - p) x (1 - guard/2)` for coordinated, `guard x (1 - score/2)` for likely_organic,
   0 for insufficient. Calibration comes from `eval/`.
7. **Enrichment** (`enrich.py`): only for members of clusters where a family already looked
   suspicious (strongest first, <= 400 accounts per pass): AppView `getProfiles` and the PLC audit
   log. Cached in `accounts`; profiles refresh after 6 h.
8. **Store** (`store.py`): see below.

## What other components read

**`clusters`**: one row per *episode* (the same accounts across consecutive windows update the
row instead of adding new ones). `campaign_id` links episodes of the same accounts over hours.
Only clusters that are `coordinated` or where some family scored >= 0.5 are stored.

`clusters.signals` (JSONB):
```json
{"detector_version": "v1.2", "score": 0.98, "p_value": 0.002,
 "family_scores": {"content": 1.0, "timing": 1.0, "network": 0.0, "identity": 0.8},
 "families_fired": ["content", "timing", "identity"],
 "null_model": {"test": "...", "statistic": "...", "observed": 0.92, "permutations": 500, "p_value": 0.002},
 "organic_guard_score": 0.0,
 "signals": [{"family": "content", "name": "duplicate_text", "score": 1.0, "polarity": "for",
              "summary": "31 of 31 accounts posted the same text", "evidence_id": 123}],
 "status_rule": {"coordinated": "...", "likely_organic": "..."}}
```

**`evidence`** with `kind='signal'`: one row per signal (and per organic guard), upserted by name
so `id`s stay stable while the episode updates. `summary` is one human sentence. `source_url` is
the first bsky.app post link. `model` = `detector:<version>`. `detail`:
```json
{"signal": "duplicate_text", "family": "content", "score": 1.0, "polarity": "for",
 "...signal-specific numbers...": 0,
 "post_uris": ["at://..."], "post_urls": ["https://bsky.app/profile/<did>/post/<rkey>"],
 "dids": ["did:plc:..."], "profile_urls": ["https://bsky.app/profile/<did>"]}
```
The analyst may cite these `evidence.id`s. If `clusters.updated_at` is newer than the analyst's
evidence row, the cluster changed and the verdict should be refreshed.

**`cluster_members(role)`**: `core` (involved in at least the median number of shared items)
or `peripheral`.
**`cluster_posts`** + **`cluster_posts_snapshot`**: the content posts behind the cluster, copied out
of the raw tables so they survive retention. Media to send to Nemotron: `media_hashes` of these
posts (blob CIDs; fetch via `com.atproto.sync.getBlob?did=<did>&cid=<cid>` on the author's PDS,
`accounts.pds_host`). Links for Oxylabs: `embed_urls` of these posts, or `detail.key` of the
`shared_link` evidence.

## Eval hooks (for `eval/`)

```python
from signals.detect import detect  # pure: WindowData -> clusters, deterministic
from signals.window import load_window  # WindowData from any DB (e.g. a replay DB)
from signals.synth import campaign, hard_negative, inject, empty_window

wd = load_window(conn, start, end, DEFAULT)  # a recorded slice replayed into a separate DB
s = campaign("L2", wd, seed=7)  # in-memory synthetic campaign, did:plc:synth...
res = detect(
    inject(wd, s), account_info=lambda dids: {d: s.accounts[d] for d in dids if d in s.accounts}
)
```
Tune on one set of seeds, report on others. `account_info` can also wrap
`signals.accounts.load_account_info` for real accounts.

## Known limits (honest list)

- Slow-drip campaigns spread over hours (L3), with aged or bought accounts and fully paraphrased
  text, mostly evade this: no burst, no duplicate text, no fresh accounts. They come out
  `insufficient`. Hijacked old accounts posting spam can even look organic.
- The p-value only tests timing. A campaign with duplicate content and fresh accounts but no
  burst is `insufficient`, not `coordinated` (identity base rates are planned).
- Independent scheduled bots (clock, weather, news feeds) look synchronized; guards catch the
  ones that post on the hour or share a declared domain, not all.
- Short texts (< 5 tokens) are ignored as content keys; copy-paste of one-liners isn't caught.
- Identity signals depend on enrichment, which is limited per pass and only for suspicious groups.
