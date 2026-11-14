# Meme swarms

One picture (a meme) and the posts that carry it. The question: is it being pushed by a swarm
(bot network, astroturfing, brigading), or did people each find it and post it? The answer comes
from *how* it was posted: timing, how alike the accounts are, reused words and files, and who
boosts whom. It never comes from one post, and a popular picture is not evidence.

- `model.py`: the contract. In: `Sighting` (one post or boost), `AccountInfo`. Out: `SwarmReport`.
- `config.py`: every threshold in `SwarmConfig`, versioned (`VERSION`, stored as `report.version`).
- `score.py`: `score_swarm(sightings, accounts, *, basis, basis_note, baseline, cfg)`. Pure: no I/O,
  seeded, and the same sightings in any order give the same report.

## Families and signals

A family's score is the **max** of its signals: signals inside a family are correlated, so they
are never added up. Each signal that needs a field counts only the accounts that have it, and its
`numbers` say how many that was. Summaries are chips: a few words with the numbers.

| Family | Signal | Chip | What it measures |
|---|---|---|---|
| timing | `burst` | `31 accounts in 4 min` | share of the accounts whose first post lands in the densest 1, 5 or 20 min, tested against a null (below) |
| timing | `same_second` | `12 accounts within 3 s` | accounts posting within 3 s of another account, tested against a null |
| timing | `clockwork` | `14 posts every 90 s` | a run of 8+ posts by 4+ accounts with near-equal gaps (a scheduler) |
| accounts | `same_cohort` | `9 of 12 joined the same week` / `20 of 25 under 30 days old` | creation dates bunched in one week (above a 25% signup-wave allowance), or more fresh accounts than the baseline |
| accounts | `templated_handles` | `11 of 14 handles alike` | one handle shape (`user482913` -> `a999999`) or one 5-letter prefix, above a 25% allowance |
| accounts | `empty_profiles` | `9 of 12 no bio or avatar` | no avatar and no bio, tested against the baseline when there is one |
| accounts | `same_bio` | `same bio x5` / `same name x5` | identical bios (3+ words) or display names, ignoring digits and emoji |
| copy | `same_text` | `same words x12` | accounts whose normalised words equal another account's, weighted by length: `(words/8)^2`, so `lol` x30 counts almost nothing |
| copy | `near_same_text` | `near-same words x16` | long texts (6+ words) that mostly overlap another account's (Jaccard >= 0.6 on word 3-shingles) |
| copy | `same_file` | `same file x26` | accounts that posted the exact same upload (equal `media_key`) |
| boost | `closed_loop` | `18 of 20 boosts from posters` | boosts of these posts that come from accounts that also posted the picture |
| boost | `fast_boost` | `boosted in 40 s x9` | the same accounts boosting 3+ different posts within 2 min each |
| boost | `repeat_posters` | `4 accounts posted 3+ times` | accounts posting the picture again and again |
| boost | `boost_ratio` | `reposts over likes on 7 of 8` | weak, only without boost sightings: public reposts above likes plus replies |

Organic guards (`polarity: "against"`); the strongest is `report.guard`:

| Guard | Chip | Fires when |
|---|---|---|
| `own_words` | `180 of 190 own words` | most accounts with text wrote 3+ words nobody else used |
| `own_files` | `150 different files` | many different uploads of the picture (people saved and re-uploaded it) |
| `established_accounts` | `160 of 170 old accounts` | over a year old with 100+ followers |
| `slow_spread` | `spread over 45 h` | many hours with no burst or clockwork; capped at 0.5, so it supports other guards but never decides alone (slow drip campaigns exist) |
| `conversation` | `12 accounts talk to each other` | posters reply to or quote each other in their own words |

## Timing null model

For each burst width `w` (60, 300, 1200 s), null worlds keep how many accounts posted in each bin
of `12 w` and move each one to a random time inside its bin (interval jitter). A slow natural rise
survives that; a spike sharper than the bin does not. `same_second` does the same with 120 s bins.
The p-value is `(1 + worlds at least as extreme) / (1 + worlds)` over up to 500 worlds, stopping
after 100 when clearly not significant. The tests are combined with Bonferroni; the smallest
adjusted p is `report.p_value`. A timing signal counts in full at p <= 0.01, for half at p <= 0.05,
and not at all above. Seconds-level tests are skipped when most times are on whole minutes.

## Verdict

```
n_accounts < 5                       -> unclear ("only 3 accounts")
score = 1 - (1 - a)(1 - b)           a, b = the two strongest family scores
fired = families scoring >= 0.5
swarm   : >= 2 fired, score >= 0.6, guard < 0.6, and if timing is one of exactly 2 fired, p <= 0.01
organic : guard >= 0.6 and <= 1 fired
unclear : everything else ("only one kind of sign", "signs both ways", "timing could be chance",
          "no strong signs either way")
```

"Unclear" is an answer, not an error: when in doubt, abstain.

## Limits

- **A sample is not the whole spread.** For `basis="sample"`, search results that are just the
  latest posts of a viral picture look like a burst. Guards (own words, own files, old accounts)
  usually catch it; without account info and text they may not.
- **Dilution.** Scores are shares of the whole set, so a swarm riding on a big organic spread is
  diluted and may come out unclear.
- **Copied captions.** People often copy a meme's original caption; that is `same_text` too. Long
  copied captions from established accounts are still held back by the guards.
- **Same file can be organic.** Re-posting the exact bytes (or a platform reusing a media key) is
  not proof; it needs a second family.
- **Slow, varied campaigns** (aged or bought accounts, paraphrased text, spread over days) look
  organic or unclear here.
- **Account fields are often missing**; the accounts family then scores 0, which is "unknown", not
  "fine".
- No signal names or labels a single account: examples are post links, numbers are counts.
