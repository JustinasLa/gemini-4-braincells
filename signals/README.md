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
   custom handle domain), `clock_aligned_posting` (top-of-the-hour schedulers),
   `members_converse` (members reply to each other's posts: a conversation, not a broadcast;
   v1.6) and `declared_automation` (accounts over 90 days old whose name or bio says bot, feed or
   automated alerts; v1.6, needs the bio text stored by migration 004).
5. **Null models** (combined with Bonferroni: p = min(1, m x smallest p) over the m tests run):
   - Timing: each member's whole activity is shifted by its own random offset (circular in the
     window), which keeps every account's own rhythm and breaks cross-account alignment. p = share
     of up to 1,000 shifts that look at least as synchronized (seeded: same input, same p).
   - Identity (since v1.5): one-sided binomial tests of fresh accounts (< 30 days) and empty
     profiles in the cluster against base rates measured on a random sample of active accounts
     (`baseline.py`, 40 per pass, last 24 h, needs >= 100 sampled; base rate smoothed (k+1)/(n+2)).
     A wave of genuinely new users raises the base rate and the test gets weaker by itself.
   `clusters.signals.null_model.combined` lists every test's p and the decisive one.
6. **Status** (deterministic, thresholds in `config.py`, version in `clusters.detector_version`):
   - `coordinated`: >= 2 families score >= 0.5, combined p <= 0.01, score >= 0.6, organic guard < 0.6
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

Data for the dashboard's visuals, also in `clusters.signals` (since v1.3):
```json
{"window": {"kind": "10m", "start": "2026-11-13T19:49:00+00:00", "end": "..."},
 "null_distribution": {"bin_edges": [0.0, 0.05, "...", 1.0], "counts": [0, 0, 51, 271, "..."],
                       "observed": 1.0, "worlds": 500, "worlds_at_least_observed": 0},
 "timeline": [{"t": 1794599361.2, "n": 3, "kind": "text", "key": "9f2c11ab", "uri": "at://..."}],
 "member_graph": {"nodes": ["did:plc:...", "..."], "edges": [[0, 3, 2, 5]], "truncated": false}}
```
- `null_distribution`: the permutation test as a picture. `counts` per bin of the synchrony
  statistic over 500 randomly time-shifted worlds; draw `observed` as a line. "Only
  `worlds_at_least_observed` of `worlds` random worlds look this synchronized." `null` when the
  cluster had too few shared actions to test.
- `timeline`: member actions on shared items in time order (<= 150, evenly sampled). `t` is epoch
  seconds (server time), `n` indexes `member_graph.nodes`, `key` groups actions on the same item
  (colour by it), `kind` is the key kind (text, neardup, media, url, like, repost, quote, follow).
- `member_graph`: `nodes` are the member DIDs (same order as `n`); `edges` are
  `[i, j, shared_content, shared_engagement]`, strongest first, <= 150. No edges for clusters
  over 150 members (`truncated: true`).

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

## Gemini: red team and semantic defence

- **Defence** (`semantic.py`, signal `content/semantic_duplicate`): Gemini embeddings
  (`gemini-embedding-001`, 768 dims, task SEMANTIC_SIMILARITY) catch accounts saying the same
  thing in different words. Only posts of candidate clusters are embedded (cached by URI, batched,
  budget per pass). An account counts when its posts match at least a third of the other accounts
  (cosine >= 0.85): density, not chaining, because same-topic organic chats chain easily.
  Calibration on Gemini-written fictional sets: at 0.85, 67% of same-campaign post pairs, 5% of
  same-topic organic pairs and 0.04% of random live pairs match. It is content evidence only:
  `coordinated` still needs a behavioural family and the timing p-value.
  The worker uses it when `GEMINI_API_KEY` is set (`--no-embeddings` turns it off).
- **Red team** (`attack.py`): Gemini 4 writes fictional campaigns (one message, many personas,
  no shared phrases) and organic look-alikes (many people, same fictional event, own opinions).
  Saved in `fixtures/gemini_attacks.json`; never posted; all accounts built from them are
  `did:plc:synth...`.
- **Eval** (`attack_eval.py`): injects them into a real live window (or `--offline`) in four
  variants (burst/spread x fresh/aged accounts) and runs the detector without and with embeddings.
  Offline result (v1.4): semantic signal fires on 32/32 campaigns (mean 0.88) and 0/16 organic
  look-alikes (mean 0.04); `coordinated` 8/8 for burst+fresh both ways, 0 for the other campaign
  variants (no timing evidence, or established accounts trip the organic guard) and 0/16 organic.
  On a real live background (5 min, 13,945 posts, `fixtures/attack_results_live_20261113T2000Z.json`):
  semantic fires on 32/32 campaigns and 0/16 look-alikes; `coordinated` 5/8 for burst+fresh (the
  other 3 get absorbed into larger live clusters), 0 for the other variants, 0/16 organic.
  With the identity base-rate test (v1.5, `fixtures/attack_results_live_v15_20261113T2000Z.json`):
  `coordinated` 8/8 burst+fresh, **8/8 spread+fresh** (was 0/8), 0/16 on aged accounts, 0/16
  organic look-alikes. Here every campaign also shares likes, so network + identity already make
  two families; the embeddings add the "same message" evidence the analyst cites. Accounts that
  only say the same thing and share no action at all never form a cluster: semantic similarity
  is not used to build the graph (cost), a known gap.

## Results summary (detector v1.6, Sat 14 Nov early morning)

| What | Result | How |
|---|---|---|
| Recall, synthetic campaigns in recorded live windows, **dev** hours (20, 22, 00 UTC) | L1 18/18, L2 17/18, L3 0/18 (all L3 shown as "can't tell") | `python -m eval recall --split dev --repeats 1 --base-rates ...` |
| Recall, **heldout** hours (19, 21, 23, 01, 03, 05 UTC; detector v1.7) | L1 26/26, L2 24/26 (92%, CI 76-98%), L3 0/26 (all shown as "can't tell") | same, `--split heldout` (nothing tuned on these hours) |
| External validation: Bluesky took down / suspended within 3 h | `coordinated` 56/458 accounts (12.2%, CI 9.5-15.6%); `likely_organic` 0/8,402; random active 0/1,200; clusters 13/81 vs 0/1,188 | `python -m eval takedowns --horizon-h 3` |
| Gemini red team (fictional paraphrase campaigns vs organic look-alikes) | fresh-account campaigns 16/16 coordinated, aged 0/16, look-alikes 0/16; semantic signal 32/32 vs 0/16 | `python -m signals.attack_eval` |
| Speed (2-vCPU budget, warm cache) | 10m pass ~5 s CPU, 1h pass ~40 s CPU + ~19 s DB; live worker 10-12 s per 10m pass | profiling in PR #34 |

The one L2 miss on dev had three families firing and p = 0.018, just above 0.01 (its accounts are
3-8 weeks old, so "under 30 days" only partly fires). Left as is: no threshold was tuned on dev.
Future work, not done because it can't be checked tonight without new labels: a rank test of
member account ages against the baseline sample (would catch weeks-old farms, but may also flag
fandoms of recent joiners).

## Error analysis (live review, Sat 14 Nov 03:30-04:00)

Every `coordinated` cluster stored by v1.5 in the previous 40 minutes (36) was read by hand:
posts, handles, ages, signals.

- Clearly right (most): affiliate-shop spam rings (Shopee "achadinhos" accounts, one link,
  paraphrased product posts), escort and porn ad rings on fresh accounts with identical media,
  OnlyFans / Instagram redirect bots with templated handles, a franchise ad network posting the
  same text and image within seconds, one spam text in several languages from templated handles.
- Wrong class 1, **openly automated public services**: `wv511*` (West Virginia 511 traffic
  alerts, 12 accounts, 8-22 months old) post the same alert in sync. They share no custom domain,
  so `transparent_automation` missed them. Fix: `declared_automation` reads the bio/name.
- Wrong class 2, **friend groups and role-play communities**, sometimes on new accounts (a K-pop
  role-play group of 2-8 day old accounts chatting in Portuguese; a fan group sending birthday
  wishes): they like each other in bursts. Measured: spam rings had 0 replies between members;
  these groups had 7-8 members replying to each other 20-60 times. Fix: `members_converse`.
  Effect on a live hour: 1 of 18 `coordinated` clusters (the role-play group) became
  `insufficient`, 1 `insufficient` became `likely_organic`; nothing else changed.
- Kept as a known limit: spam from old, followed accounts (bought or hijacked) looks organic on
  identity and timing.

Earlier fixes from the same kind of review: weather feeds sharing a custom handle domain
(`transparent_automation`, v1.1) and hourly clock bots (`clock_aligned_posting`, v1.2).

## Known limits (honest list)

- Slow-drip campaigns spread over hours (L3), with aged or bought accounts and fully paraphrased
  text, mostly evade this: no burst, no duplicate text, no fresh accounts. They come out
  `insufficient`. Hijacked old accounts posting spam can even look organic.
- Campaigns on aged or bought accounts, spread over time, still have no significant test (timing
  and identity both look normal): they stay `insufficient` even when Gemini embeddings show the
  same message.
- Before v1.5 the p-value only tested timing. A campaign with duplicate content and fresh accounts but no
  burst is `insufficient`, not `coordinated` (identity base rates are planned).
- Independent scheduled bots (clock, weather, news feeds) look synchronized; guards catch the
  ones that post on the hour or share a declared domain, not all.
- Short texts (< 5 tokens) are ignored as content keys; copy-paste of one-liners isn't caught.
- Identity signals depend on enrichment, which is limited per pass and only for suspicious groups.
