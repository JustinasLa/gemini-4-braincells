# Methodology and limits

How Shill or Chill decides that a group of Bluesky accounts is acting together, how sure it is,
when it refuses to decide, how we measure our own error rates, and what it can't do.

This page describes the code in this repository. Where a value is a setting, the file that holds
it is named; that file is authoritative if it changes. Measured accuracy is not written here: it
is computed by `eval/` and served live by `GET /api/metrics`.

Status: ingest, the detector, Oxylabs source checks, Nemotron media reading, Gemini verdicts and the
API are on `main` and running live, and so is the evaluation harness (its results are at
`/api/metrics`).

## What we detect

We detect **coordinated amplification**: several accounts that post, repost, like, reply or follow
in a way that independent people would rarely produce by chance. Typical cases: the same text or
link pushed by a ring of new accounts within seconds, reply floods with one message under many
threads, or accounts that keep liking the same posts together.

We do **not** judge whether a message is true, false, political, popular or offensive. A single
post, however suspicious its wording, is not evidence of coordination. Coordination is a property
of several accounts acting together, so every claim we make is about a group's behaviour.

## Verdicts

Every cluster gets exactly one status. The status is decided by fixed rules in `signals/`, never
by a language model.

| API value (`clusters.status`) | Label shown to users | Meaning |
|---|---|---|
| `coordinated` | 🚩 Shill | Several independent signal families agree, the timing can't be explained by chance, and no strong organic explanation is present. |
| `likely_organic` | 😎 Chill | A strong organic explanation is present (for example established accounts, or a wider crowd doing the same thing) and at most one family looks suspicious. |
| `insufficient` | 🤷 Can't tell (yet) | Everything else. We abstain: the evidence doesn't support either call. |

"Can't tell" is a result, not an error. We would rather abstain than flag people who are just
reacting to the same news.

Abstaining has a cost, and we measure it. Some 🤷 clusters meet every 🚩 condition except the
p-value. Bluesky's own moderation later takes down their accounts about as often as accounts in
🚩 clusters (`eval takedowns`, row `insufficient_p_only`). The other 🤷 accounts, 😎 accounts and
random active accounts are taken down far less, or never. We still show these clusters as 🤷:
without a test that beats chance, a flag would be a guess. So we miss some real campaigns rather
than accuse people we can't show acted together.

## Pipeline overview

```
Bluesky Jetstream ──▶ ingest/ ──▶ Postgres (posts, reposts, likes, follows, account events)
                                        │
                           signals/ (every minute: 10 min window; every 5 min: 1 h window)
                                        │  keys → account graph → clusters → signals → status
                                        ▼
                          clusters + evidence(kind=signal) ── the decision is made here
                                        │
          ┌─────────────────────────────┼──────────────────────────────┐
          ▼                             ▼                              ▼
  sources/ (Oxylabs)           media/ (Nemotron VL)           analyst/ (Gemini)
  evidence(kind=source)        evidence(kind=media)           evidence(kind=analyst)
  what linked pages say,       what images/frames show        explains the status, cites
  where the text appears       (OCR, reuse cues)              evidence ids, counter-evidence
          └─────────────────────────────┴──────────────────────────────┘
                                        ▼
                         api/ (FastAPI) ──▶ web dashboard
```

## Data and time windows

- **Source**: Bluesky's public Jetstream firehose (posts, reposts, likes, follows, account and
  identity events). Read-only; we never post, like, follow or create accounts.
- **Clock**: windows use `seen_at`, the Jetstream server time, not the post's own `createdAt`
  (clients can backdate that).
- **Windows**: a 10-minute window every minute and a 1-hour window every 5 minutes
  (`signals/config.py`, `windows`). A window contains its posts, plus the likes, reposts and follows
  whose target was hit by several distinct accounts in that window, plus per-second background
  event counts.
- **Episodes**: when the same accounts keep going across windows, the existing cluster row is
  updated instead of adding a new one, and `campaign_id` links episodes of the same accounts over
  hours. A cluster's status can change while it updates (see Known failure modes).

## Step 1: shared keys

A key is something several accounts did with the same item (`signals/keys.py`):

- **Content keys**: the same normalised text (short texts are ignored, because one-liners like
  "thanks" collide organically), near-duplicate text by MinHash over word 3-shingles, the same
  uploaded image or video file (identical uploads share a blob CID), the same normalised link.
- **Engagement keys**: liking, reposting or quoting the same post; following the same account.

Keys used by too many accounts in the window (`max_key_accounts`) are dropped. A link shared by
thousands of people during breaking news can't tell coordination from popularity, so it never
creates an edge. This is the first organic guard, and it is structural.

## Step 2: account graph and clusters

Accounts are nodes. Two accounts get an edge when they share at least one content key, or several
engagement keys (`min_engagement_shared`). Each shared key adds weight `1/ln(1+n)`, where `n` is how
many accounts used it, so rare shared items count more than common ones. Clusters are the connected
components; components over 30 accounts are split into communities with seeded Leiden (50 before v1.10, which let a ring hide inside a crowd sharing its link). Clusters smaller
than `min_cluster_size` are ignored, and so are Leiden communities larger than `max_cluster_size`
(`signals/graph.py`).

## Step 3: signal families

Each cluster is scored on four independent families (`signals/score.py`). A family's score is the
**maximum** of its signals, never the sum: signals inside one family are correlated (copy-pasted
text is also near-duplicate text), and adding them would count the same evidence twice.

| Family | Signals | What it measures |
|---|---|---|
| content | `duplicate_text`, `near_duplicate_text`, `duplicate_media`, `shared_link`, `semantic_duplicate` | share of members posting the same thing, times how exclusive the item is to this cluster; `semantic_duplicate`: posts that say the same thing in different words (Gemini embeddings `gemini-embedding-001`, cosine >= 0.85; an account counts when it matches at least a third of the other accounts; computed for candidate clusters only) |
| timing | `burst_synchrony`, `clockwork_posting` | members acting within the same tight time span; accounts acting at near-constant intervals |
| network | `repeated_co_engagement`, `reply_flood`, `mutual_amplification` | pairs engaging with the same posts again and again; one text replied under many threads; members amplifying each other |
| identity | `fresh_accounts`, `empty_profiles`, `templated_handles`, `shared_small_pds` | young accounts, no avatar/bio/followers, handles from one template, many accounts on one small independent server |

Every signal is stored as an `evidence` row with a one-sentence summary, its numbers, and links
to the posts and profiles it is about (bsky.app URLs), so every claim can be checked by hand.

Identity signals need account metadata (profile and PLC creation date). Fetching it is limited,
so it is done only for members of clusters where some family already looked suspicious, strongest
first (`signals/enrich.py`).

## Step 4: two null tests and how they are combined

Synchronised bursts and fresh, empty accounts are the core evidence, and organic crowds produce
both too. So they are tested against null models instead of fixed thresholds.

### (a) Timing

1. **Statistic**: for every shared item with enough member actions, count the members' actions
   inside the densest `sync_seconds` span; pool over items and divide by all member actions on
   those items. A value of 1.0 means every action happened in tight bursts.
2. **Null worlds**: shift each member's *whole* activity in the window by its own random offset
   (circularly). This keeps every account's own rhythm (how often and in what pattern it posts)
   and breaks only the alignment *between* accounts.
3. **p-value**: `(1 + worlds at least as synchronised as observed) / (1 + worlds)`, over up to
   `permutations` worlds, stopping early once p is clearly not significant. The random generator
   is seeded from the cluster and window, so the same input always gives the same p.

The burst signal only counts as strong when p is small (`max_p_coordinated`); a marginal p only
counts for half, and a clearly non-significant p counts for nothing. The histogram of the null
worlds and the observed value are stored with the cluster (`signals.null_distribution`), so the
dashboard can draw "only k of N random worlds look this synchronised".

### (b) Identity (since v1.5)

Are fresh accounts or empty profiles over-represented in the cluster compared with ordinary
active accounts (`signals/baseline.py`, `identity_tests` in `signals/score.py`)?

1. **Base rates**: every pass samples 40 random active accounts from the window (seeded, so a
   replay samples the same ones) and enriches them like cluster members. Over the last 24 h, the
   base rates are the share of fresh accounts (under 30 days) and of empty profiles (no avatar, no
   bio, under 5 followers). A test runs only once at least 100 sampled accounts have data.
2. **Test**: one-sided binomial test of the cluster's count against the smoothed base rate
   `(k+1)/(n+2)`, so a small sample never claims a zero base rate. A wave of genuinely new users
   raises the base rate, and the test gets weaker by itself.
3. Bridged fediverse accounts (Bridgy Fed) don't count for age: their creation date is the date
   the bridge was set up, so their age is unknown.

### (c) Combining the tests

The tests that could run are combined with Bonferroni: `p = min(1, m × min p_i)` over the `m`
available tests. Every test's p and the decisive one are stored in
`signals.null_model.combined`. The `clusters.p_value` column stores at least 1e-30 (a REAL column
can't hold smaller values); the exact value stays in the JSON.

Content has no null model yet (see limits).

## Step 5: organic guards (counter-evidence)

Guards are signals with `polarity: "against"`. They are stored and shown like any other evidence,
as reasons the cluster might be organic:

| Guard | Fires when |
|---|---|
| `established_accounts` | most members are over a year old with a real follower count |
| `part_of_wider_crowd` | the items this cluster shares are also used by many accounts outside it |
| `language_diversity` | members post in several languages. Since v1.10 it counts only for the share of accounts that aren't new: a translator makes a 12-language campaign easy, so many languages argue for independent people only when the accounts have a past |
| `transparent_automation` | members openly share one custom handle domain (one declared operator, such as a feed service) and are not new. The domain is the registered one from the Public Suffix List (v1.9), so people with their own `name.co.uk` handles are not grouped |
| `clock_aligned_posting` | shared posts cluster right after full or half hours, the pattern of independent schedulers |
| `members_converse` | members reply to each other's posts: a conversation, not a broadcast (spam rings we reviewed had 0 such replies) |
| `declared_automation` | the name or bio of most accounts over 90 days old says bot, feed or automated alerts; only accounts whose bio was loaded count |

The strongest guard is the cluster's `organic_guard_score`.

## Step 6: score, status rule and confidence

- **Score** = noisy-OR of the two strongest family scores: `1 - (1 - a)(1 - b)`. A third or fourth
  family can't push it further than two show.
- **Status** (thresholds in `signals/config.py`; the version is stored on every cluster as
  `detector_version`):
  - `coordinated` needs **all** of: at least `min_families_coordinated` families fired (score at
    or above `family_fire`), a combined p-value (timing and identity tests) at or below `max_p_coordinated`, a score at or above
    `min_score_coordinated`, and the organic guard below `organic_guard_strong`.
  - `likely_organic`: the organic guard is strong and at most one family fired.
  - `insufficient`: everything else, including strong signals with a strong guard. Conflicting
    evidence means we abstain.
- **Confidence** is a heuristic, not yet calibrated: `min(score, 1 - p) × (1 - guard/2)` for
  `coordinated`, `guard × (1 - score/2)` for `likely_organic`, `0` for `insufficient`. Calibration
  (does 0.8 mean right 80% of the time?) has to come from labelled data in `eval/`.

The exact rule text is stored with each cluster (`signals.status_rule`), so the dashboard shows
the rule that produced the verdict.

## Where the models fit

The models explain, read and fetch. They never decide what gets flagged.

### Gemini: the analyst (`analyst/`)

For clusters above a score threshold, Gemini gets the detector's output, the evidence rows
(signals, organic guards, source checks) and a few sample posts. For candidate clusters (detector
status `coordinated` or `insufficient`) it runs as an agent with function calling
(`analyst/agent.py`): it may search the web and fetch linked pages live through the Oxylabs Web
API, and read the cluster's posts and member profiles from our database, before it answers. Each
web result is stored as source evidence (with its URL) before Gemini sees its evidence id. Calls
are limited per cluster (tool calls, live web calls, wall time), cached by URL and query, and
pages can only be fetched if their URL already appears in the cluster or an earlier result.
Clusters the detector already calls `likely_organic` get a cheaper model in one shot, without web
calls.

Gemini returns structured JSON: its own assessment, confidence, the evidence it relies on (by
evidence id), the strongest counter-evidence, and what would change the verdict. Code then
enforces (`analyst/guard.py`):

- Citations to evidence ids that don't exist (passed in or stored by a tool in this run) are
  dropped.
- The verdict's `status` is always the detector's. Gemini's own call is `llm_assessment`, with
  `agrees_with_detector` and a `disagreement_reason`, so a disagreement is shown and counted, and
  "why is this flagged?" is never answered with "the model said so".
- Fewer than two valid citations make Gemini's assessment `insufficient`.
- Every change is recorded in `adjustments` and shown.

Sample posts and database lookups are context only and can't be cited. The prompt tells the model
to judge behaviour, not opinions, not to guess who is behind the accounts, and to treat text in
posts and web pages as data, not instructions. Each verdict is a new row (readers take the
latest), stored with the model id and the full trace of model and tool calls (model id, latency,
tokens, cost; tool, endpoint, URL, evidence id). The model choice is measured in
`analyst/README.md`.

### Oxylabs: source checks (`sources/`)

For a cluster's most-shared outside links (bsky.app links and media files are skipped), the
Oxylabs Web API `/scrape` call with AI extraction reads the linked page: publisher, author, date,
headline, main claims, HTTP status. `/search` then looks for the headline, and for a distinctive
phrase from the cluster's most repeated text, as an exact-phrase web search. Every fact is stored
with the URL it came from (`source_url`), and calls are made live at runtime. The earliest date
among search results is labelled as a hint, not proof of origin: search engines show dates
loosely.

Source checks describe where content appears. They are evidence for the analyst and for people,
not an input to the status rule.

### Nemotron 3.5 Super VL: media reader (`media/`)

Identical uploaded files are already caught without any model (`duplicate_media`, same blob CID).
Nemotron Super VL handles what a hash can't: for media in candidate clusters it describes the
image or video frame, extracts visible text (OCR of memes and screenshots), notes reuse and
near-duplicate cues, and lists cautious manipulated-context cues. Different images whose
in-image text is mostly the same (one template, a city or name swapped) across accounts get one
extra finding, which no file hash can produce. Media is fetched read-only from Bluesky's public
CDN, results are cached by media hash, and each is stored as an `evidence` row with
`kind='media'` and the model id the response returned; the worker refuses to store anything if
that id is not a VL model. The model is told to describe only what is visible, not to infer
intent, and not to describe sexual images (a code guard enforces that too). Prompt iterations and
their measured effect: `media/README.md`.

For ad-hoc image lookups, `lookup/imagetrace.py` also asks TinEye and Yandex through Oxylabs
for visual candidates, independently of OCR. Downloaded candidates are compared in code;
unrelated pictures are dropped, and Nemotron describes changes in edited/cropped copies.
Every request has a receipt. Downloads check public DNS addresses, pin the checked IP, check
redirects, and bound time/bytes. Optional comparison modules may be unavailable; then candidates
remain explicitly `unknown`. The separate `image_origin_id` requires a same/resized/cropped
match whose first-seen upper bound predates every dated verified post that may carry the picture
(the submitted post and its copies with an image, or any post showing one of the picture's own
copies; not text-only copies, the author's other posts or replies). TinEye crawl dates say
the picture is at least that old, not when it was published or who created it; day-only dates
use the following midnight as a conservative bound. Text origin and coordination status do
not change. Visual differences are described as **edited**, with the observed changes; an
`edited` relation without listed changes is a pixel difference only (a strong colour filter can
cause one), never on its own evidence that the content changed.

## Ad-hoc checks: `POST /api/analyze`

Anyone can submit a handle, link or text. The answer keeps two questions apart:

1. **Is it part of a cluster we monitor?** If the handle is a member, the link was shared by a
   cluster, or the text appears in a cluster's posts, the response is `matched` and lists those
   clusters: their detector status and analyst verdict apply.
2. **Otherwise** the response is `insufficient`, with no verdict. Live Oxylabs findings about
   the link or text may be attached. They describe where the content appears, and the response
   says this is weaker evidence. One post or one link can't show coordination, so we don't
   pretend it can.

Very short texts aren't searched (they match too much of the web).

## How the metrics are made

Every accuracy number the app shows is a row in the `eval_runs` table written by `eval/`, served
as-is by `GET /api/metrics` (latest row per dataset and split). Nothing is typed into the UI by
hand. Each row's `details` records the detector version, the eval code commit, the windows used
and the per-case outcomes, so a number can be traced back to what produced it.

### Data and splits

- **Recordings**: raw Jetstream traffic recorded by `ingest --record`, replayed offline. The
  windows are rebuilt in memory with the same normaliser and the same selection rules as the live
  worker, and scored with the live detector entry point (`signals.detect.detect`). No database, no
  network writes.
- **Split rule**: a window belongs to the UTC hour it starts in. **Odd hours are held out; even
  hours are dev.** Thresholds and generator settings may be tuned on dev only; reported numbers
  come from held-out hours. Whole hours keep one burst on one side of the split.
- Partial or ad-hoc runs can't be published as a split's result.
- If `/api/metrics` returns an empty list, no run has been published to that deployment yet, so there
  are no accuracy numbers to show yet.

### Datasets

| `dataset` | Measures | Ground truth |
|---|---|---|
| `synthetic_campaigns` | **recall**: share of injected campaigns marked `coordinated`, with a 95% Wilson interval, per difficulty level | synthetic campaigns we generate (`signals/synth.py`) and inject into real recorded windows, one at a time; true by construction |
| `organic_synth` | **false-positive rate** on synthetic hard negatives: viral posts, news links, live events, fandoms | synthetic, true by construction |
| `organic_live` | **false-positive rate** on real organic bursts in held-out recordings | picked by a fixed rule that never reads detector output; heuristic until a human checks a sample |

Campaign levels: **L1** copy-paste text and link from brand-new templated accounts in a burst
under a minute; **L2** light paraphrase, accounts weeks old, a few minutes of jitter; **L3** heavy
paraphrase, aged accounts with followers, slow drip across the whole window.

For the organic sets, each case's outcome is the worst status among clusters that contain its
accounts: `coordinated` (a false positive) > `likely_organic` > `insufficient` > not clustered.
Besides the plain false-positive rate we also report it among cases that were clustered at all,
the harder number, because "never grouped" cases are easy wins.

### What we can't measure yet

- **Precision on live data** needs hand labels of live clusters (two labellers, agreement
  reported). Until those exist, no live precision number is shown.
- **Calibration** of `confidence` needs the same labels.
- Synthetic recall is an upper bound for campaigns that look like our generator, not a measured
  recall on real operations.

## Known failure modes and limits

These are the cases we know the detector gets wrong or can't see. Several were found by running
it on live traffic.

- **Slow-drip campaigns** spread over hours, run with aged or bought accounts and fully
  paraphrased text, mostly evade it: no burst, no duplicate text, no fresh accounts. They come out
  `insufficient`. Hijacked old accounts posting spam can even look organic.
- **Content has no null model.** A campaign spread over time on aged accounts has no significant
  test (timing and identity both look normal), so it stays `insufficient`, even when its content
  is duplicated or Gemini embeddings show the same message.
- **Declared automation looks coordinated.** Independent schedulers (clocks, weather, news feeds)
  and one publisher's set of topic-feed accounts post the same thing at the same second. Guards
  catch schedulers that post on the hour, operators that share a custom handle domain, and aged
  accounts that call themselves bots or feeds in their name or bio, not all of them. Such clusters can sit on the boundary and change status while they update.
- **Very large rings aren't scored.** A Leiden community bigger than `max_cluster_size` is
  dropped, so one huge ring that the graph can't split into smaller communities is never shown.
- **Piggybacking on viral items is invisible.** Items used by too many accounts are dropped on
  purpose (that is what protects breaking news), so a ring that only likes a post that is already
  viral creates no edges.
- **Short texts** are ignored as content keys, so copy-paste of one-liners isn't caught.
- **Scripts without spaces** (Chinese, Japanese, Thai) are tokenised by whitespace, so long texts
  in them count as very short: no text keys and no exact-phrase web search for them.
- **Enrichment budget**: identity signals and the established-accounts guard depend on profile
  data fetched for a limited number of accounts per pass. Missing data weakens both directions.
- **Coverage**: we see what Jetstream delivers while we're connected; gaps in the stream are gaps
  in the evidence. Deleted posts are kept as soft deletes (deleting after amplifying is itself a
  pattern), but we only see deletes that happen while we watch.
- **Confidence isn't calibrated yet** (see above).
- **Models can be wrong.** Gemini explains, and code checks that it only cites real evidence ids,
  but its wording can still be imperfect. Nemotron's descriptions and OCR can misread images. Web
  search dates are hints.

## Ethics

- **Behaviour, not opinions.** Signals measure how accounts act together, never what they believe
  or say. A political or controversial message is not evidence, and agreeing with one isn't
  suspicious.
- **Coordination, not intent.** We can show that accounts acted in concert. We can't show that
  anyone was paid, who runs them, or why. Evidence text describes behaviour ("31 accounts, same
  link, within 94 s").
- **No labels on people.** "Shill" is only the name of the product and the label of a cluster's
  verdict. No account or person is ever called "a shill", by the code, the evidence text or the
  analyst prompt.
- **Counter-evidence is always shown**, together with confidence and the rule that produced the
  verdict. Abstaining is preferred to a weak flag.
- **Read-only.** We never post, like, follow, reply, report or create accounts on Bluesky, and test
  campaigns exist only in offline replays, labelled synthetic everywhere.
- **No doxxing.** We only use public data that Bluesky serves to everyone (posts, handles, public
  profiles, PLC records). We don't try to identify the people behind accounts, link accounts to
  real-world identities or combine them with other data sources about individuals. Links point to
  the public posts, so anyone can check them.
- **Data retention.** Raw firehose data is deleted after a short retention window. Only posts that
  a cluster references are kept, as evidence for that cluster.
