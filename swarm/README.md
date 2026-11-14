# Meme swarms

One picture (a meme) and the posts that carry it. The question: is it being pushed by a swarm
(bot network, astroturfing, brigading), or did people each find it and post it? The answer comes
from *how* it was posted: timing, how alike the accounts are, reused words and files, and who
boosts whom. It never comes from one post, and a popular picture is not evidence.

- `model.py`: the contract. In: `Sighting` (one post or boost), `AccountInfo`. Out: `SwarmReport`.
- `config.py`: every threshold in `SwarmConfig`, versioned (`VERSION`, stored as `report.version`).
- `score.py`: `score_swarm(sightings, accounts, *, basis, basis_note, baseline, selected_by_file,
  window, cfg)`. Pure: no I/O, seeded, and the same sightings in any order give the same report.
  - `selected_by_file=True`: the caller picked the group because one exact file was posted by many
    accounts (the live pass). That file is the selection rule, not evidence, so `same_file` and
    `own_files` are not computed and a verdict needs two other families.
  - `window=(start, end)`: the stretch the caller watched completely. Timing null worlds stay inside
    [first post, window end]; without a window, inside [first post, last post].
  - The same post seen under several ids (same account, kind, target, second, file and words) is
    counted once; boosts of a dropped copy point at the kept one.

## Families and signals

A family's score is the **max** of its signals: signals inside a family are correlated, so they
are never added up. Share-based scores are the **Wilson 95% lower bound** of the share, not the
raw share, so small groups need a near unanimous share ("5 of 5" scores about 0.57, "43 of 43"
about 0.92). Each signal that needs a field counts only the accounts that have it, and its
`numbers` say how many that was. Summaries are chips: a few words with the numbers. Every signal
counts accounts, so one busy account cannot bring a family to 0.5. Only timing and copy signals
carry `examples` (post links); accounts and boost signals and the guards never do.

| Family | Signal | Chip | What it measures |
|---|---|---|---|
| timing | `burst` | `31 accounts in 4 min` | accounts whose first post lands in the densest 1, 5 or 20 min, tested against a null (below) |
| timing | `same_second` | `12 accounts paired within 3 s` | accounts with a post within 3 s of another account's, tested against a null |
| timing | `clockwork` | `14 posts every 90 s` | a run of 8+ posts by 4+ accounts with near-equal gaps (a scheduler). A threshold, not a test: it has no p-value |
| accounts | `same_cohort` | `20 of 25 under 30 days old` / `9 of 12 joined the same week` | more accounts under 30 days old than the baseline rate. Fires only with a baseline (n >= 100), binomial p <= 0.01 and 8+ accounts with a known age; otherwise capped at 0.45. "Joined the same week" is only the wording, used when 4+ accounts (and at least as many as are fresh) were created within one week, counting only accounts created in the 60 days before the first post; an old signup wave never counts |
| accounts | `templated_handles` | `11 of 14 handles alike` | one handle shape with digits (`user482913` -> `a999999`), above a 25% allowance; needs 6+ handles |
| accounts | `empty_profiles` | `9 of 12 no bio or avatar` | no avatar and no bio; same rule as `same_cohort` (baseline test, p <= 0.01, 8+ accounts to fire, else capped at 0.45) |
| accounts | `same_bio` | `same bio x5` / `same name x5` | identical bios (3+ words) or display names, ignoring digits and emoji |
| copy | `same_text` | `same words x12` | accounts whose normalised words equal another account's, weighted by length: `(words/8)^2`, so `lol` x30 counts almost nothing |
| copy | `near_same_text` | `near-same words x16` | long texts (6+ words) that mostly overlap another account's (Jaccard >= 0.6 on word 3-shingles) |
| copy | `same_file` | `same file x26` | accounts that posted the exact same upload (equal `media_key`); not computed with `selected_by_file` |
| boost | `closed_loop` | `5 of 6 boosters posted it` | reposts, quotes and replies of these posts come from accounts that posted the picture too (likes never count: friends like each other) |
| boost | `fast_boost` | `4 accounts boost 9 posts fast` | 3+ accounts that each boost 3+ different posts within 2 min; a post counts when 2+ of them did |
| boost | `repeat_posters` | `4 accounts posted it 3+ times` | 3+ accounts posting the picture again and again |
| boost | `boost_ratio` | `reposts over likes: 7 of 8` | weak, only without boost sightings: accounts whose post has public reposts above likes plus replies |

Organic guards (`polarity: "against"`); the strongest is `report.guard`:

| Guard | Chip | Fires when |
|---|---|---|
| `own_words` | `180 of 190 own words` | most accounts with text wrote 3+ words nobody else used |
| `own_files` | `150 different files` | many different uploads of the picture; capped at 0.5 (bots re-upload too), skipped for X (every X upload gets a new media key) and with `selected_by_file` |
| `established_accounts` | `160 of 170 old accounts` | over a year old with 100+ followers |
| `slow_spread` | `spread over 45 h` | many hours with no burst or clockwork; capped at 0.5, so it supports other guards but never decides alone (slow drip campaigns exist) |
| `conversation` | `12 accounts talk to each other` | posters reply to or quote each other in their own words |

## Timing null model

For each burst width `w` (60, 300, 1200 s), null worlds keep how many accounts posted in each bin
of `12 w` and move each one to a random time inside its bin (interval jitter). A slow natural rise
survives that; a spike sharper than the bin does not. `same_second` does the same with 120 s bins.
Bins start at the first post and are clipped at the end of `window` (or at the last post without
one): null worlds never place posts where nobody looked, so a set of posts that all lie in a short
stretch (a search's newest 20 posts) cannot show a significant burst. The p-value is
`(1 + worlds at least as extreme) / (1 + worlds)` over up to 500 worlds, stopping after 100 when
clearly not significant. The tests are combined with Bonferroni; the smallest adjusted p is
`report.p_value`. A timing signal counts in full at p <= 0.01, for half at p <= 0.05, and not at
all above. Seconds-level tests are skipped when 80%+ of times are on whole minutes. On synthetic
posting with no coordination the rate of p <= 0.01 is 0/200 (20 posts in 15 min), 1/200 (30 in
1 h) and 2/200 (60 in 24 h).

## Verdict

```
n_accounts < 5                       -> unclear ("only 3 accounts")
score = 1 - (1 - a)(1 - b)           a, b = the two strongest family scores
fired = families scoring >= 0.5
swarm   : >= 2 fired, score >= 0.6, guard < 0.6, and if timing is one of exactly 2 fired, the
          timing signal that gave the family its score has p <= 0.01 (clockwork has no p, so it
          needs two other families)
organic : guard >= 0.6, no family fired, and (without a window) the timed posts span 20+ min
unclear : everything else ("only one kind of sign", "signs both ways", "timing could be chance",
          "posts span only minutes", "no strong signs either way")
```

"Unclear" is an answer, not an error: when in doubt, abstain. A fired family with strong guards
is "signs both ways", never "organic".

## Limits

- **A sample is not the whole spread.** For `basis="sample"` without a window, timing only sees
  structure inside the span the sample covers; a burst that is the whole sample cannot be told
  from a search that returned the newest posts, so it does not count. Such a sample is also never
  called organic when it spans under 20 minutes.
- **Natural cascades.** One big account posting a picture makes a real spike that the null can
  call significant. It needs a second, non-timing family to become a swarm.
- **Dilution.** Scores are shares of the whole set, so a swarm riding on a big organic spread is
  diluted and may come out unclear.
- **Copied captions.** People often copy a meme's original caption; that is `same_text` too. Long
  copied captions from established accounts are still held back by the guards.
- **Same file can be organic.** Re-posting the exact bytes (or a platform reusing a media key) is
  not proof; it needs a second family, and with `selected_by_file` it is not counted at all.
- **Small groups.** With the Wilson bound, a group of 5 to 7 accounts needs every account to
  agree for a share signal to fire; tested account signals need 8+ accounts.
- **Slow, varied campaigns** (aged or bought accounts, paraphrased text, spread over days) look
  organic or unclear here.
- **Account fields are often missing**, and without a baseline the account shares are capped
  below 0.5; the accounts family is then "unknown", not "fine".
- **Hand-set thresholds.** Every value in `config.py` was set on synthetic cases
  (`tests/test_swarm_score.py`); there is no measured error rate on labelled swarms yet.
- No signal names or labels a single account: examples are post links (timing and copy only),
  numbers are counts.
