"""Meme swarm scorer: is one picture being pushed by a swarm of accounts?

Pure and deterministic: no I/O, seeded random numbers, the same input (in any order) always gives
the same report. Four families, each scored as the max of its signals (signals inside a family are
correlated, so they are never added up), plus organic guards that point the other way:

  timing    burst, same_second (tested against a null), clockwork (a threshold)
  accounts  same_cohort, empty_profiles (tested against a baseline), templated_handles, same_bio
  copy      same_text, near_same_text, same_file (not when the group was picked by its file)
  boost     closed_loop, fast_boost, repeat_posters, boost_ratio
  guards    own_words, own_files, established_accounts, slow_spread, conversation

Share-based scores are the Wilson 95% lower bound of the share, so a small group needs a near
unanimous share to score at all ("5 of 5" ~0.57, "43 of 43" ~0.92).

Verdict, by rule:

  swarm    >= min_families_swarm families fire, score >= min_score_swarm, guard < guard_strong,
           and when timing is one of only two families fired, the p-value of the timing signal
           that gave the family its score is <= max_p_swarm (clockwork has none: it cannot be one
           of only two)
  organic  no family fired and guard >= guard_strong (and, without an observation window, the
           timed posts span at least organic_min_span_s)
  unclear  everything else, and always below min_accounts posting accounts

A popular picture is not evidence: no signal counts posts or likes on their own. Signals describe
the group; none names or labels a single account, one account's actions never bring a family to
family_fire, and only timing and copy signals carry example post links.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache

import numpy as np

from signals.textnorm import normalize_text, shingles, word_count
from swarm.config import SwarmConfig
from swarm.model import (
    FAMILIES,
    AccountInfo,
    Basis,
    Burst,
    Family,
    Sighting,
    SwarmReport,
    SwarmSignal,
    TimelinePoint,
)

DAY = 86400.0
EXAMPLE_FAMILIES: frozenset[str] = frozenset({"timing", "copy"})  # the others never link posts


@dataclass(frozen=True)
class Baseline:  # base rates among ordinary active accounts, when known
    fresh_rate: float | None = None  # share of accounts younger than cfg.fresh_account_days
    empty_rate: float | None = None  # share with no bio and no avatar
    n: int = 0


# ---------------------------------------------------------------- helpers ---------------------
def _r(x: float) -> float:
    return round(float(x), 4)


def _sig(p: float) -> float:
    return float(f"{float(p):.3g}")


def _ts(dt: datetime | None) -> float | None:
    """Epoch seconds; a naive datetime is taken as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _dt(t: float) -> datetime:
    return datetime.fromtimestamp(t, tz=UTC)


def _dur(s: float) -> str:
    """A span in plain words: 40 s, 4 min, 3 h, 2 days."""
    s = max(0.0, float(s))
    if s < 120:
        return f"{max(1, round(s))} s"
    if s < 90 * 60:
        return f"{round(s / 60)} min"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h"
    return f"{round(s / DAY)} days"


def _accounts_word(n: int) -> str:
    return f"{n} account{'' if n == 1 else 's'}"


def _wilson(k: float, n: int, z: float) -> float:
    """Wilson score lower bound of the share k / n (k may be fractional: a weighted count)."""
    if n <= 0 or k <= 0:
        return 0.0
    p = min(1.0, k / n)
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomial(n, p). Exact sum in log space (n is at most a few hundred)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = min(max(p, 1e-12), 1 - 1e-12)
    lp, lq = math.log(p), math.log1p(-p)
    lf = math.lgamma(n + 1)
    terms = [
        lf - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        for i in range(k, n + 1)
    ]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(x - m) for x in terms))


def _factor(p: float | None, cfg: SwarmConfig) -> float:
    if p is None:
        return 0.0
    return 1.0 if p <= cfg.p_strong else 0.5 if p <= cfg.p_weak else 0.0


@lru_cache(maxsize=8192)
def _norm_words(text: str) -> tuple[str, int]:
    """(normalize_text, word_count), cached: swarms repeat the same words, and this is the
    slowest step."""
    norm = normalize_text(text)
    return norm, word_count(norm)


def _norm(text: str) -> str:
    return _norm_words(text)[0]


def _text_weight(words: int, cfg: SwarmConfig) -> float:
    return min(1.0, words / cfg.text_full_words) ** 2


@dataclass
class _Post:
    id: str
    account: str
    platform: str
    t: float | None  # exact time, for timing signals
    t_any: float | None  # any time, for account ages
    norm: str
    words: int
    media_key: str | None
    likes: int | None
    reposts: int | None
    replies: int | None


@dataclass
class _Ctx:
    cfg: SwarmConfig
    posts: list[_Post]
    boosts: list[Sighting]
    accounts: list[str]  # posting accounts, in order of first post
    posts_by_account: dict[str, list[_Post]]
    info: dict[str, AccountInfo]
    ref_t: dict[str, float]  # per posting account: the time its age is measured at
    rng: np.random.Generator
    first_t: float | None = None  # the group's first post (any time)
    window_end: float | None = None  # end of the observation window, epoch seconds
    selected_by_file: bool = False
    # filled by the copy step, used by guards and the timeline
    text_groups: dict[str, int] = field(default_factory=dict)  # norm text -> group number
    reusing: set[str] = field(default_factory=set)  # accounts sharing words with another
    near: set[str] = field(default_factory=set)

    @property
    def n(self) -> int:
        return len(self.accounts)

    def wilson(self, k: float, n: int) -> float:
        return _wilson(k, n, self.cfg.wilson_z)

    def examples(self, accounts) -> list[str]:
        """First post of each of these accounts, in posting order (links, never names)."""
        acc = set(accounts)
        out = [self.posts_by_account[a][0].id for a in self.accounts if a in acc]
        return out[: self.cfg.max_examples]


def _signal(
    family: Family, name: str, score: float, summary: str, numbers: dict, examples=(), against=False
):
    # privacy: only timing and copy "for" signals link posts; accounts, boost and guards never do
    keep = family in EXAMPLE_FAMILIES and not against
    return SwarmSignal(
        family=family,
        name=name,
        score=_r(min(1.0, max(0.0, score))),
        polarity="against" if against else "for",
        summary=summary,
        numbers=numbers,
        examples=list(examples) if keep else [],
    )


# ---------------------------------------------------------------- timing ----------------------
def _densest(t: np.ndarray, w: float) -> tuple[int, int]:
    """t sorted -> (most values within any span of width w, index of the first of them)."""
    if len(t) == 0:
        return 0, 0
    counts = np.searchsorted(t, t + w, side="right") - np.arange(len(t))
    i = int(np.argmax(counts))
    return int(counts[i]), i


def _reaches(t: np.ndarray, k: int, w: float) -> np.ndarray:
    """t: (P, n) sorted rows -> (P,) whether some k values of the row fit in a span of width w."""
    if k <= 1:
        return np.ones(t.shape[0], dtype=bool)
    return (t[:, k - 1 :] - t[:, : t.shape[1] - k + 1]).min(axis=1) <= w


def _bin_index(t: np.ndarray, width: float, hi: float) -> np.ndarray:
    """Bins of `width` from 0 (the first post); the last bin is the one that holds `hi`."""
    last = max(0, math.ceil(hi / width) - 1) if hi > 0 else 0
    return np.minimum(np.floor(t / width), last).astype(np.int64)


def _jitter(
    t: np.ndarray, width: float, hi: float, rows: int, rng: np.random.Generator
) -> np.ndarray:
    """Interval jitter: every time moves to a random place inside its own bin of `width`, clipped
    to [0, hi] (first post .. end of what was observed). Posts are never placed where nobody
    looked: after the window closed, or before the picture's first post."""
    a = _bin_index(t, width, hi) * width
    span = np.minimum(a + width, hi) - a
    return a[None, :] + span[None, :] * rng.random((rows, len(t)), dtype=np.float32)


def _can_group(t: np.ndarray, width: float, hi: float, k: int) -> np.ndarray:
    """Mask of the times that can be among k inside one window no wider than `width` in some
    jittered world. A time never leaves its bin, and such a window touches at most two adjacent
    bins, so a time whose bin plus either neighbour holds fewer than k times never counts.
    Dropping those before simulating changes no world's outcome; it only saves work."""
    b = _bin_index(t, width, hi)
    uniq, inv, cnt = np.unique(b, return_inverse=True, return_counts=True)

    def count_of(x: np.ndarray) -> np.ndarray:
        idx = np.minimum(np.searchsorted(uniq, x), len(uniq) - 1)
        return np.where(uniq[idx] == x, cnt[idx], 0)

    c0 = cnt[inv]
    return (c0 + count_of(b - 1) >= k) | (c0 + count_of(b + 1) >= k)


def _perm_test(reach, jitter, t: np.ndarray, cfg: SwarmConfig, rng) -> tuple[float, int]:
    """(1 + worlds at least as extreme) / (1 + worlds), stopping early when clearly large.
    reach(worlds) -> bool per world: at least as extreme as observed; jitter(t, rows, rng) -> the
    null worlds. `t` may be empty when no world can reach the observed value: then
    p = 1 / (1 + permutations) without simulating."""
    if len(t) == 0:
        return 1 / (1 + cfg.permutations), cfg.permutations
    done = hits = 0
    for batch in (cfg.early_stop_after, cfg.permutations - cfg.early_stop_after):
        if batch <= 0:
            break
        hits += int(reach(jitter(t, batch, rng)).sum())
        done += batch
        if (1 + hits) / (1 + done) > cfg.early_stop_p:
            break
    return (1 + hits) / (1 + done), done


def _near_accounts_stat(labels: np.ndarray, n_acc: int, s: float):
    """Statistic for same_second: accounts with a post within s seconds of another account's."""

    def stat(tt: np.ndarray) -> np.ndarray:
        order = np.argsort(tt, axis=1, kind="stable")
        ts = np.take_along_axis(tt, order, axis=1)
        lab = labels[order]
        close = (np.diff(ts, axis=1) <= s) & (lab[:, 1:] != lab[:, :-1])
        flag = np.zeros(ts.shape, dtype=bool)
        flag[:, :-1] |= close
        flag[:, 1:] |= close
        hit = np.zeros((ts.shape[0], n_acc), dtype=bool)
        rows = np.repeat(np.arange(ts.shape[0]), ts.shape[1])
        f = flag.ravel()
        hit[rows[f], lab.ravel()[f]] = True
        return hit.sum(axis=1)

    return stat


def _timing(ctx: _Ctx) -> tuple[list[SwarmSignal], float | None, Burst | None, dict]:
    cfg = ctx.cfg
    out: list[SwarmSignal] = []
    timed = sorted((p for p in ctx.posts if p.t is not None), key=lambda p: (p.t, p.id))
    first: dict[str, float] = {}
    for p in timed:
        first.setdefault(p.account, p.t)
    info: dict = {"timed_accounts": len(first)}
    if len(first) < 2:
        info["span_s"] = 0.0
        return out, None, None, info
    t0 = timed[0].t
    # null worlds stay inside [first post, end of the window] (or the last post without one)
    hi = timed[-1].t - t0
    if ctx.window_end is not None:
        hi = max(hi, ctx.window_end - t0)
    info["span_s"] = timed[-1].t - t0
    acc_order = sorted(first, key=lambda a: (first[a], a))
    ft = np.array([first[a] - t0 for a in acc_order])  # sorted, one time per account
    n_t = len(ft)
    tests: dict[str, float] = {}
    runs = 0

    # --- burst: densest window of first posts, at several widths, vs interval-jitter nulls ---
    scales = []
    for w in cfg.burst_windows_s:
        scales.append({"w": w, "obs": _densest(ft, w)[0], "p": None, "worlds": 0})
    if n_t >= cfg.min_burst_accounts:
        for sc in scales:
            if sc["obs"] < cfg.min_burst_accounts:
                continue

            def reach(tt, k=sc["obs"], w=sc["w"]):
                if tt.shape[1] < k:
                    return np.zeros(tt.shape[0], dtype=bool)
                return _reaches(np.sort(tt, axis=1), k, w)

            jw = sc["w"] * cfg.burst_jitter_factor
            live = ft[_can_group(ft, jw, hi, sc["obs"])]
            sc["p"], sc["worlds"] = _perm_test(
                reach, lambda t, rows, rng, jw=jw: _jitter(t, jw, hi, rows, rng), live, cfg, ctx.rng
            )
            runs += sc["worlds"]

    # --- same_second: accounts posting within a few seconds of another account ---
    all_t = np.array([p.t - t0 for p in timed])
    whole_minutes = np.mod(np.round(all_t + t0, 3), 60.0) == 0
    coarse = float(np.mean(whole_minutes)) >= cfg.coarse_clock_share
    info["coarse_clock"] = coarse
    ss_obs = 0
    ss_p = None
    if not coarse and n_t >= cfg.min_burst_accounts:
        lab_index = {a: i for i, a in enumerate(acc_order)}
        labels = np.array([lab_index[p.account] for p in timed])
        ss_obs = int(_near_accounts_stat(labels, n_t, cfg.same_second_s)(all_t[None, :])[0])
        if ss_obs >= cfg.min_same_second_accounts:
            # times alone in their neighbourhood never get a near neighbour: leave them out
            jw = cfg.same_second_jitter_s
            keep = _can_group(all_t, jw, hi, 2)
            stat = _near_accounts_stat(labels[keep], n_t, cfg.same_second_s)
            ss_p, worlds = _perm_test(
                lambda tt: stat(tt) >= ss_obs,
                lambda t, rows, rng: _jitter(t, jw, hi, rows, rng),
                all_t[keep],
                cfg,
                ctx.rng,
            )
            runs += worlds

    # Bonferroni over the timing tests that ran
    for sc in scales:
        if sc["p"] is not None:
            tests[f"burst_{sc['w']}s"] = sc["p"]
    if ss_p is not None:
        tests["same_second"] = ss_p
    m = len(tests)
    adj = {k: min(1.0, m * v) for k, v in tests.items()}
    p_value = min(adj.values()) if adj else None
    info["tests"] = {k: _sig(v) for k, v in sorted(adj.items())}
    info["worlds"] = runs

    # densest stretch: the most significant width, else the middle one
    tested = [sc for sc in scales if sc["p"] is not None]
    if tested:
        best = min(tested, key=lambda sc: (adj[f"burst_{sc['w']}s"], -sc["obs"], sc["w"]))
    else:
        best = scales[len(scales) // 2]
    w = best["w"]
    count, i = _densest(ft, w)
    j = i + count - 1
    start, end = ft[i] + t0, ft[j] + t0
    in_burst = [p for p in timed if start <= p.t <= end]
    burst = Burst(
        start=_dt(start),
        end=_dt(end),
        n_posts=len(in_burst),
        n_accounts=len({p.account for p in in_burst}),
    )
    if best["p"] is not None:
        p_adj = adj[f"burst_{w}s"]
        k = best["obs"]
        out.append(
            _signal(
                "timing",
                "burst",
                ctx.wilson(k, n_t) * _factor(p_adj, cfg),
                f"{k} accounts in {_dur(end - start)}",
                {
                    "accounts": k,
                    "timed_accounts": n_t,
                    "window_s": w,
                    "span_s": _r(end - start),
                    "p_value": _sig(p_adj),
                    "worlds": best["worlds"],
                },
                ctx.examples(acc_order[i : j + 1]),
            )
        )
    if ss_p is not None:
        p_adj = adj["same_second"]
        out.append(
            _signal(
                "timing",
                "same_second",
                ctx.wilson(ss_obs, n_t) * _factor(p_adj, cfg),
                # each account has a post seconds from another's, not all in one moment
                f"{ss_obs} accounts paired within {int(cfg.same_second_s)} s",
                {"accounts": ss_obs, "timed_accounts": n_t, "p_value": _sig(p_adj)},
            )
        )

    # --- clockwork (a threshold, not a test): a long run of near-equal gaps between different
    # accounts' posts ---
    if not coarse and len(timed) >= cfg.clockwork_min_posts:
        best_run: tuple[int, int] = (0, 0)  # (start index, gaps)
        k = 0
        while k < len(timed) - 1:
            g0 = timed[k + 1].t - timed[k].t
            if g0 < cfg.clockwork_min_gap_s or timed[k + 1].account == timed[k].account:
                k += 1
                continue
            tol = cfg.clockwork_tol * g0 + cfg.clockwork_tol_s
            e = k + 1
            while e < len(timed) - 1:
                g = timed[e + 1].t - timed[e].t
                if abs(g - g0) > tol or timed[e + 1].account == timed[e].account:
                    break
                e += 1
            if e - k > best_run[1]:
                best_run = (k, e - k)
            k = e
        s0, gaps = best_run
        run = timed[s0 : s0 + gaps + 1]
        run_acc = {p.account for p in run}
        if len(run) >= cfg.clockwork_min_posts and len(run_acc) >= cfg.clockwork_min_accounts:
            gap = (run[-1].t - run[0].t) / gaps
            out.append(
                _signal(
                    "timing",
                    "clockwork",
                    ctx.wilson(len(run_acc), n_t) * min(1.0, gaps / cfg.clockwork_full_gaps),
                    f"{len(run)} posts every {_dur(gap)}",
                    {
                        "posts": len(run),
                        "accounts": len(run_acc),
                        "gap_s": _r(gap),
                        "timed_accounts": n_t,
                    },
                    [p.id for p in run][: cfg.max_examples],
                )
            )
    return out, p_value, burst, info


# ---------------------------------------------------------------- accounts --------------------
_LETTERS = re.compile(r"[^\W\d_]+")
_DIGITS = re.compile(r"\d+")


def _profile_norm(v: str) -> str:
    """Bio or display name -> its letter words, lowercased, digits and emoji dropped
    ("Crypto fan 🚀 #4821" -> "crypto fan"). Lighter than normalize_text: only compared for
    equality."""
    return " ".join(_LETTERS.findall(unicodedata.normalize("NFKC", v).casefold()))


def _local(handle: str) -> tuple[str, str]:
    h = handle.strip().lower().lstrip("@")
    if h.startswith("x:"):
        h = h[2:]
    local, _, domain = h.partition(".")
    return local, domain


def _handle_shape(handle: str) -> str:
    """amber4821.bsky.social -> a9999.bsky.social (as in signals/score.py, but digit runs keep
    their length)."""
    local, domain = _local(handle)
    s = _DIGITS.sub(lambda m: "9" * len(m.group()), _LETTERS.sub("a", local))
    return f"{s}.{domain}" if domain else s


def _tested_share(
    ctx: _Ctx, k: int, n: int, rate: float | None, baseline: Baseline | None
) -> tuple[float, float | None]:
    """(score, p) of a share tested against a baseline rate. It fires (full Wilson bound) only
    with a baseline, p <= p_strong and n >= min_tested_accounts; a weaker or untested share is
    capped below family_fire; a share the baseline calls ordinary (p > p_weak) scores 0."""
    cfg = ctx.cfg
    lb = ctx.wilson(k, n)
    if baseline is None or baseline.n < cfg.baseline_min_n or rate is None:
        return min(cfg.no_baseline_cap, lb), None
    p0 = (rate * baseline.n + 1) / (baseline.n + 2)
    p = _binom_sf(k, n, p0)
    if p <= cfg.p_strong and n >= cfg.min_tested_accounts:
        return lb, p
    if p <= cfg.p_weak:
        return min(cfg.no_baseline_cap, lb), p
    return 0.0, p


def _accounts(ctx: _Ctx, baseline: Baseline | None) -> list[SwarmSignal]:
    cfg = ctx.cfg
    out: list[SwarmSignal] = []
    known = [(a, ctx.info[a]) for a in ctx.accounts if a in ctx.info]
    base_ok = baseline is not None and baseline.n >= cfg.baseline_min_n

    # same_cohort: more fresh accounts than the baseline. A same-week bunch is only the wording
    # when the fresh accounts also joined together; an old signup wave is ordinary.
    created = sorted(
        ((_ts(i.created_at), a) for a, i in known if i.created_at is not None),
        key=lambda x: (x[0], x[1]),
    )
    if len(created) >= cfg.min_known:
        aged = [(c, a) for c, a in created if a in ctx.ref_t]
        fresh = [a for c, a in aged if ctx.ref_t[a] - c < cfg.fresh_account_days * DAY]
        if len(aged) >= cfg.min_known and fresh:
            rate = baseline.fresh_rate if baseline is not None else None
            score, p = _tested_share(ctx, len(fresh), len(aged), rate, baseline)
            if score > 0:
                k = 0
                if ctx.first_t is not None:
                    recent = np.array(
                        [c for c, _ in aged if ctx.first_t - c <= cfg.cohort_recent_days * DAY]
                    )
                    k = _densest(recent, cfg.cohort_days * DAY)[0]
                bunch = k >= cfg.min_cohort_accounts and k >= len(fresh)
                week = "the same week" if cfg.cohort_days == 7 else f"within {cfg.cohort_days} days"
                out.append(
                    _signal(
                        "accounts",
                        "same_cohort",
                        score,
                        f"{k} of {len(aged)} joined {week}"
                        if bunch
                        else f"{len(fresh)} of {len(aged)} under {cfg.fresh_account_days} days old",
                        {
                            "same_week_recent": k,
                            "fresh": len(fresh),
                            "with_age": len(aged),
                            "fresh_p_value": _sig(p) if p is not None else None,
                            "baseline_fresh_rate": baseline.fresh_rate if base_ok else None,
                        },
                    )
                )

    # templated_handles: one handle shape with digits (user482913 -> a999999)
    handles = [(a, i.handle) for a, i in known if i.handle and i.handle.lower() != "handle.invalid"]
    if len(handles) >= cfg.min_template_handles:
        shapes: dict[str, list[str]] = defaultdict(list)
        for a, h in handles:
            s = _handle_shape(h)
            if "9" in s.split(".")[0]:
                shapes[s].append(a)
        best = max(sorted(shapes), key=lambda s: len(shapes[s]), default=None)
        top = shapes[best] if best else []
        lb = ctx.wilson(len(top), len(handles))
        score = max(0.0, (lb - cfg.handle_background) / (1 - cfg.handle_background))
        if len(top) >= cfg.min_template_accounts and score > 0:
            out.append(
                _signal(
                    "accounts",
                    "templated_handles",
                    score,
                    f"{len(top)} of {len(handles)} handles alike",
                    {"alike": len(top), "with_handle": len(handles), "shape": best},
                )
            )

    # empty_profiles: no avatar and no bio, tested against the baseline
    prof = [(a, i) for a, i in known if i.has_avatar is not None]
    if len(prof) >= cfg.min_known:
        empty = [a for a, i in prof if not i.has_avatar and not (i.bio or "").strip()]
        rate = baseline.empty_rate if baseline is not None else None
        score, p = _tested_share(ctx, len(empty), len(prof), rate, baseline) if empty else (0, None)
        if score > 0:
            out.append(
                _signal(
                    "accounts",
                    "empty_profiles",
                    score,
                    f"{len(empty)} of {len(prof)} no bio or avatar",
                    {
                        "empty": len(empty),
                        "with_profile": len(prof),
                        "p_value": _sig(p) if p is not None else None,
                        "baseline_empty_rate": baseline.empty_rate if base_ok else None,
                    },
                )
            )

    # same_bio: near-identical bios or display names (digits dropped, normalised)
    best_sig = None
    for kind, attr in (("bio", "bio"), ("name", "name")):
        keyed = []
        for a, i in known:
            v = getattr(i, attr)
            if not v or not v.strip():
                continue
            keyed.append((a, _profile_norm(v)))
        if len(keyed) >= cfg.min_known:
            groups: dict[str, list[str]] = defaultdict(list)
            for a, norm in keyed:
                if kind == "bio" and len(norm.split()) < cfg.bio_min_words:
                    continue
                if kind == "name" and len(norm.replace(" ", "")) < cfg.name_min_chars:
                    continue
                groups[norm].append(a)
            if not groups:
                continue
            g = max(sorted(groups), key=lambda s: len(groups[s]))
            members = groups[g]
            if len(members) < cfg.min_same_bio_accounts:
                continue
            weight = (
                min(1.0, len(g.split()) / cfg.bio_full_words) if kind == "bio" else cfg.name_weight
            )
            score = ctx.wilson(len(members), len(keyed)) * weight
            if best_sig is None or score > best_sig.score:
                best_sig = _signal(
                    "accounts",
                    "same_bio",
                    score,
                    f"same {kind} x{len(members)}",
                    {"kind": kind, "alike": len(members), f"with_{kind}": len(keyed)},
                )
    if best_sig is not None and best_sig.score > 0:
        out.append(best_sig)
    return out


# ---------------------------------------------------------------- copy ------------------------
def _copy(ctx: _Ctx) -> list[SwarmSignal]:
    cfg = ctx.cfg
    out: list[SwarmSignal] = []
    by_text: dict[str, set[str]] = defaultdict(set)
    words: dict[str, int] = {}
    for p in ctx.posts:
        if p.norm:
            by_text[p.norm].add(p.account)
            words[p.norm] = p.words
    shared = {t: acc for t, acc in by_text.items() if len(acc) >= 2}
    ranked = sorted(shared, key=lambda t: (-len(shared[t]), t))
    ctx.text_groups = {t: i + 1 for i, t in enumerate(ranked)}

    # same_text: weighted by length, so "lol" x30 weighs far less than one long sentence x30
    best_w: dict[str, float] = {}
    for t, acc in shared.items():
        w = _text_weight(words[t], cfg)
        for a in acc:
            best_w[a] = max(best_w.get(a, 0.0), w)
    ctx.reusing = set(best_w)
    if shared:
        top = ranked[0]
        score = ctx.wilson(sum(best_w.values()), ctx.n) * min(
            1.0, len(best_w) / cfg.min_copy_accounts
        )
        out.append(
            _signal(
                "copy",
                "same_text",
                score,
                f"same words x{len(shared[top])}",
                {
                    "accounts": len(shared[top]),
                    "words": words[top],
                    "accounts_reusing": len(best_w),
                    "groups": len(shared),
                    "posting_accounts": ctx.n,
                },
                ctx.examples(shared[top]),
            )
        )

    # near_same_text: long texts whose word 3-shingles mostly overlap another account's
    long_texts = sorted(
        (t for t in by_text if words[t] >= cfg.near_min_words), key=lambda t: (-len(by_text[t]), t)
    )[: cfg.near_max_texts]
    near_acc: set[str] = set()
    variants = 0
    if len(long_texts) >= 2:
        sh = [shingles(t) for t in long_texts]
        df = Counter(g for s in sh for g in s)
        # only shingles in >= 2 texts can make an overlap: own words leave a tiny matrix
        vocab = {g: i for i, g in enumerate(sorted(g for g, c in df.items() if c >= 2))}
        pairs: list[tuple[int, int]] = []
        if vocab:
            mat = np.zeros((len(long_texts), len(vocab)), dtype=np.float32)
            for i, s in enumerate(sh):
                cols = [vocab[g] for g in s if g in vocab]
                if cols:
                    mat[i, cols] = 1.0
            inter = mat @ mat.T
            size = np.array([len(s) for s in sh], dtype=np.float32)
            jac = inter / np.maximum(size[:, None] + size[None, :] - inter, 1.0)
            np.fill_diagonal(jac, 0.0)
            pairs = list(zip(*np.nonzero(jac >= cfg.near_jaccard), strict=True))
        for i, j in pairs:
            a_i, a_j = by_text[long_texts[i]], by_text[long_texts[j]]
            if len(a_i | a_j) >= 2:  # two different accounts, not one account's variants
                near_acc |= a_i | a_j
                variants += 1
        for t in long_texts:  # exact long copies are near copies too
            if len(by_text[t]) >= 2:
                near_acc |= by_text[t]
    ctx.near = near_acc
    if variants and len(near_acc) > len(
        {a for t in long_texts if len(by_text[t]) >= 2 for a in by_text[t]}
    ):
        score = (
            ctx.wilson(len(near_acc), ctx.n)
            * cfg.near_weight
            * min(1.0, len(near_acc) / cfg.min_copy_accounts)
        )
        out.append(
            _signal(
                "copy",
                "near_same_text",
                score,
                f"near-same words x{len(near_acc)}",
                {
                    "accounts": len(near_acc),
                    "posting_accounts": ctx.n,
                    "similar_pairs": variants // 2,
                },
                ctx.examples(near_acc),
            )
        )

    # same_file: many accounts posted the exact same upload. Not when the caller picked the group
    # by that file: then it is the selection rule, not evidence.
    if not ctx.selected_by_file:
        by_file: dict[str, set[str]] = defaultdict(set)
        for p in ctx.posts:
            if p.media_key:
                by_file[p.media_key].add(p.account)
        with_media = {a for acc in by_file.values() for a in acc}
        if by_file:
            key = max(sorted(by_file), key=lambda k: len(by_file[k]))
            k = len(by_file[key])
            if k >= 2:
                score = ctx.wilson(k, len(with_media)) * min(1.0, k / cfg.min_copy_accounts)
                out.append(
                    _signal(
                        "copy",
                        "same_file",
                        score,
                        f"same file x{k}",
                        {"accounts": k, "with_file": len(with_media), "files": len(by_file)},
                        ctx.examples(by_file[key]),
                    )
                )
    return out


# ---------------------------------------------------------------- boost -----------------------
def _boost(ctx: _Ctx) -> list[SwarmSignal]:
    """Every boost signal counts accounts, never actions, so one busy account cannot fire it."""
    cfg = ctx.cfg
    out: list[SwarmSignal] = []
    by_id = {p.id: p for p in ctx.posts}
    posters = set(ctx.accounts)
    on_set = [b for b in ctx.boosts if b.target in by_id and b.account != by_id[b.target].account]

    # closed_loop: the boosters (reposts, quotes, replies; likes are what friends do) are the
    # posters themselves. Share of boosting accounts, scaled down below min_loop_accounts.
    loopable = [b for b in on_set if b.kind != "like"]
    if len(loopable) >= cfg.min_boosts:
        boosters = {b.account for b in loopable}
        loop_acc = boosters & posters
        if loop_acc:
            score = ctx.wilson(len(loop_acc), len(boosters)) * min(
                1.0, len(loop_acc) / cfg.min_loop_accounts
            )
            out.append(
                _signal(
                    "boost",
                    "closed_loop",
                    score,
                    f"{len(loop_acc)} of {len(boosters)} boosters posted it",
                    {
                        "boosters_who_posted": len(loop_acc),
                        "boosters": len(boosters),
                        "boosts": len(loopable),
                    },
                )
            )

    # fast_boost: several accounts boost post after post within seconds or minutes. A post counts
    # only when at least fast_boost_min_per_post of those accounts boosted it fast.
    fast: dict[str, set[str]] = defaultdict(set)
    delays: dict[tuple[str, str], float] = {}
    boosted: set[str] = set()
    for b in on_set:
        tb = _ts(b.at) if b.time_exact else None
        tp = by_id[b.target].t
        if tb is None or tp is None:
            continue
        boosted.add(b.target)
        d = tb - tp
        if 0 <= d <= cfg.fast_boost_s:
            fast[b.account].add(b.target)
            key = (b.account, b.target)
            delays[key] = min(d, delays.get(key, d))
    regular = {a for a, ts in fast.items() if len(ts) >= cfg.fast_boost_min_targets}
    per_post = Counter(t for a in regular for t in fast[a])
    hit = {t for t, c in per_post.items() if c >= cfg.fast_boost_min_per_post}
    if len(regular) >= cfg.min_fast_boosters and len(hit) >= cfg.fast_boost_min_targets:
        med = float(np.median([d for (a, t), d in delays.items() if a in regular and t in hit]))
        score = ctx.wilson(len(hit), len(boosted)) * min(1.0, len(hit) / cfg.fast_boost_full_posts)
        out.append(
            _signal(
                "boost",
                "fast_boost",
                score,
                f"{len(regular)} accounts boost {len(hit)} posts fast",
                {
                    "posts": len(hit),
                    "posts_boosted": len(boosted),
                    "boosters": len(regular),
                    "median_s": _r(med),
                },
            )
        )

    # repeat_posters: several accounts post the picture again and again
    rep = [a for a in ctx.accounts if len(ctx.posts_by_account[a]) >= cfg.repeat_min_posts]
    if len(rep) >= cfg.min_repeat_accounts:
        out.append(
            _signal(
                "boost",
                "repeat_posters",
                ctx.wilson(len(rep), ctx.n),
                f"{_accounts_word(len(rep))} posted it {cfg.repeat_min_posts}+ times",
                {
                    "accounts": len(rep),
                    "posting_accounts": ctx.n,
                    "posts_by_them": sum(len(ctx.posts_by_account[a]) for a in rep),
                },
            )
        )

    # boost_ratio (weak, only without boost sightings): reposts above likes and replies, counted
    # per account (an account counts when its post with the most reposts looks like that)
    if not ctx.boosts:
        top: dict[str, _Post] = {}
        for p in ctx.posts:
            if p.reposts is None or p.likes is None or p.reposts < cfg.boost_ratio_min_reposts:
                continue
            if p.account not in top or (p.reposts, p.id) > (
                top[p.account].reposts,
                top[p.account].id,
            ):
                top[p.account] = p
        if len(top) >= cfg.boost_ratio_min_accounts:
            odd = [a for a, p in top.items() if p.reposts > p.likes + (p.replies or 0)]
            if odd:
                out.append(
                    _signal(
                        "boost",
                        "boost_ratio",
                        cfg.boost_ratio_weight * ctx.wilson(len(odd), len(top)),
                        f"reposts over likes: {len(odd)} of {len(top)}",
                        {"accounts": len(odd), "with_counts": len(top)},
                    )
                )
    return out


# ---------------------------------------------------------------- guards ----------------------
def _guards(ctx: _Ctx, timing_score: float) -> list[SwarmSignal]:
    cfg = ctx.cfg
    out: list[SwarmSignal] = []

    # own_words: most accounts wrote words nobody else used
    with_text = [a for a in ctx.accounts if any(p.norm for p in ctx.posts_by_account[a])]
    if len(with_text) >= cfg.min_accounts:
        own = [
            a
            for a in with_text
            if a not in ctx.reusing
            and a not in ctx.near
            and any(p.words >= cfg.own_words_min_words for p in ctx.posts_by_account[a])
        ]
        coverage = min(1.0, len(with_text) / (cfg.min_text_coverage * ctx.n))
        score = ctx.wilson(len(own), len(with_text)) * coverage
        if own:
            out.append(
                _signal(
                    "copy",
                    "own_words",
                    score,
                    f"{len(own)} of {len(with_text)} own words",
                    {"own": len(own), "with_text": len(with_text), "posting_accounts": ctx.n},
                    against=True,
                )
            )

    # own_files: many different uploads of the picture (people saved and re-uploaded it). Capped
    # below guard_strong (bots re-upload too); not for X, where every upload gets a new media key,
    # and not when the caller picked the group by one file.
    if not ctx.selected_by_file:
        files: dict[str, str] = {}
        for p in ctx.posts:
            if p.media_key and p.platform != "x":
                files.setdefault(p.account, p.media_key)
        if len(files) >= cfg.min_accounts:
            distinct = len(set(files.values()))
            out.append(
                _signal(
                    "copy",
                    "own_files",
                    min(cfg.own_files_max, ctx.wilson(distinct - 1, len(files) - 1)),
                    f"{distinct} different files",
                    {"files": distinct, "with_file": len(files)},
                    against=True,
                )
            )

    # established_accounts: over a year old with followers
    aged = [
        (a, i)
        for a in ctx.accounts
        if (i := ctx.info.get(a)) is not None
        and i.created_at is not None
        and i.followers is not None
        and a in ctx.ref_t
    ]
    if len(aged) >= cfg.min_known:
        est = [
            a
            for a, i in aged
            if ctx.ref_t[a] - _ts(i.created_at) >= cfg.established_days * DAY
            and i.followers >= cfg.established_followers
        ]
        if est:
            out.append(
                _signal(
                    "accounts",
                    "established_accounts",
                    ctx.wilson(len(est), len(aged)),
                    f"{len(est)} of {len(aged)} old accounts",
                    {"established": len(est), "with_age": len(aged)},
                    against=True,
                )
            )

    # slow_spread: many hours or days, no burst, no clockwork
    first: dict[str, float] = {}
    for p in sorted((p for p in ctx.posts if p.t is not None), key=lambda p: (p.t, p.id)):
        first.setdefault(p.account, p.t)
    if len(first) >= cfg.min_accounts and timing_score < cfg.family_fire:
        ft = np.array(sorted(first.values()))
        span = float(ft[-1] - ft[0])
        if span >= cfg.slow_spread_h * 3600:
            peak = _densest(ft, 3600.0)[0] / len(ft)
            out.append(
                _signal(
                    "timing",
                    "slow_spread",
                    cfg.slow_spread_max
                    * min(1.0, span / (cfg.slow_spread_full_h * 3600))
                    * (1 - peak),
                    f"spread over {_dur(span)}",
                    {"span_s": _r(span), "timed_accounts": len(ft), "peak_hour_share": _r(peak)},
                    against=True,
                )
            )

    # conversation: posters reply to or quote each other in their own words
    by_id = {p.id: p for p in ctx.posts}
    posters = set(ctx.accounts)
    talk = [
        b
        for b in ctx.boosts
        if b.kind in ("reply", "quote")
        and b.account in posters
        and b.target in by_id
        and by_id[b.target].account != b.account
    ]
    texts = Counter(_norm(b.text) for b in talk)
    own_talk = [
        b
        for b in talk
        if (n := _norm(b.text))
        and _norm_words(b.text)[1] >= cfg.own_words_min_words
        and texts[n] == 1
        and n not in ctx.text_groups
    ]
    talkers = {b.account for b in own_talk}
    if len(talkers) >= cfg.min_conversing_accounts:
        out.append(
            _signal(
                "boost",
                "conversation",
                min(1.0, 2 * ctx.wilson(len(talkers), ctx.n)),
                f"{len(talkers)} accounts talk to each other",
                {"accounts": len(talkers), "replies": len(own_talk), "posting_accounts": ctx.n},
                against=True,
            )
        )
    return out


# ---------------------------------------------------------------- timeline --------------------
def _timeline(
    ctx: _Ctx, sightings: list[Sighting], burst: Burst | None, flag_burst: bool
) -> list[TimelinePoint]:
    cfg = ctx.cfg
    index = {a: i for i, a in enumerate(ctx.accounts)}
    norm_of = {p.id: p.norm for p in ctx.posts}
    lo = _ts(burst.start) if burst and flag_burst else None
    hi = _ts(burst.end) if burst and flag_burst else None
    pts = []
    for s in sightings:
        t = _ts(s.at) if s.time_exact else None
        if t is None:
            continue
        if s.account not in index:
            index[s.account] = len(index)
        g = ctx.text_groups.get(norm_of.get(s.id, ""), 0) if s.kind == "post" else 0
        b = lo is not None and s.kind == "post" and lo <= t <= hi
        pts.append((t, index[s.account], s.id, s.kind, g, b))
    pts.sort(key=lambda x: (x[0], x[1], x[2]))
    if len(pts) > cfg.timeline_max:
        idx = np.linspace(0, len(pts) - 1, cfg.timeline_max).round().astype(int)
        pts = [pts[i] for i in sorted(set(idx.tolist()))]
    return [TimelinePoint(t=round(t, 3), a=a, k=k, g=g, burst=b) for t, a, _, k, g, b in pts]


# ---------------------------------------------------------------- main ------------------------
def _seed(cfg: SwarmConfig, sightings: list[Sighting]) -> int:
    h = hashlib.blake2b(digest_size=8)
    h.update(f"{cfg.seed}|{cfg.version}".encode())
    for s in sightings:
        h.update(f"|{s.id}|{s.account}|{_ts(s.at)}".encode())
    return int.from_bytes(h.digest(), "big")


def _order_key(s: Sighting) -> tuple:
    """A total order on sightings by content, so duplicates resolve the same in any input order."""
    t = _ts(s.at)
    return (t is None, t or 0.0, s.id, s.model_dump_json())


def _dedupe(sightings: list[Sighting]) -> list[Sighting]:
    """One sighting per id, then one per real action: the same post seen under several ids (a
    URL and an x:<id>, two search results) has the same account, kind, target, second, file and
    words, and is kept once; boosts of a dropped copy point at the kept one."""
    by_id: dict[str, Sighting] = {}
    for s in sorted(sightings, key=_order_key):
        by_id.setdefault(s.id, s)
    ordered = sorted(by_id.values(), key=_order_key)

    def action(s: Sighting, target: str | None) -> tuple:
        t = _ts(s.at)
        return (
            s.account,
            s.kind,
            target,
            None if t is None else round(t),
            s.media_key or None,
            _norm(s.text),
        )

    alias: dict[str, str] = {}
    kept: list[Sighting] = []
    seen: dict[tuple, str] = {}
    for s in [x for x in ordered if x.kind == "post"] + [x for x in ordered if x.kind != "post"]:
        target = alias.get(s.target, s.target) if s.target else None
        key = action(s, target)
        if key in seen:
            alias[s.id] = seen[key]
            continue
        seen[key] = s.id
        kept.append(s if target == s.target else s.model_copy(update={"target": target}))
    return sorted(kept, key=_order_key)


def _why_unclear(
    fired: list[str], guard: float, timing_p_ok: bool, short: bool, cfg: SwarmConfig
) -> str:
    strong = guard >= cfg.guard_strong
    if len(fired) >= cfg.min_families_swarm and strong:
        return "signs both ways"
    if len(fired) >= cfg.min_families_swarm and not timing_p_ok:
        return "timing could be chance"
    if fired and strong:
        return "signs both ways"
    if len(fired) == 1:
        return "only one kind of sign"
    if strong and short:
        return "posts span only minutes"
    return "no strong signs either way"


def score_swarm(
    sightings: list[Sighting],
    accounts: dict[str, AccountInfo] | None = None,  # keyed by Sighting.account
    *,
    basis: Basis = "sample",
    basis_note: str = "",
    baseline: Baseline | None = None,
    selected_by_file: bool = False,
    window: tuple[datetime, datetime] | None = None,
    cfg: SwarmConfig = SwarmConfig(),  # noqa: B008  (frozen dataclass: safe as a default)
) -> SwarmReport:
    """Score one picture's sightings.

    selected_by_file: the caller picked this group because one exact file was posted by many
    accounts; that file is then the selection rule, so same_file and own_files are not computed.
    window: the stretch the caller watched completely (the live pass window). Timing null worlds
    stay inside [first post, window end]; without it, inside [first post, last post], so a sample
    of the newest posts cannot show a significant burst."""
    info = dict(accounts or {})
    ordered = _dedupe(sightings)
    tmap: dict[str, float | None] = {s.id: _ts(s.at) for s in ordered}

    posts: list[_Post] = []
    boosts: list[Sighting] = []
    for s in ordered:
        if s.kind != "post":
            boosts.append(s)
            continue
        norm, words = _norm_words(s.text)
        posts.append(
            _Post(
                id=s.id,
                account=s.account,
                platform=s.platform,
                t=tmap[s.id] if s.time_exact else None,
                t_any=tmap[s.id],
                norm=norm,
                words=words,
                media_key=s.media_key or None,
                likes=s.likes,
                reposts=s.reposts,
                replies=s.replies,
            )
        )

    first_post: dict[str, tuple] = {}
    posts_by_account: dict[str, list[_Post]] = defaultdict(list)
    for p in posts:
        posts_by_account[p.account].append(p)
        key = (p.t is None, p.t if p.t is not None else (p.t_any or 0.0), p.id)
        if p.account not in first_post or key < first_post[p.account]:
            first_post[p.account] = key
    accs = sorted(first_post, key=lambda a: (*first_post[a], a))

    exact = [t for s in ordered if s.time_exact and (t := tmap[s.id]) is not None]
    anyt = [t for s in ordered if (t := tmap[s.id]) is not None]
    times = exact or anyt
    first_at = _dt(min(times)) if times else None
    last_at = _dt(max(times)) if times else None
    ref_t: dict[str, float] = {}
    for a in accs:
        ts = [p.t_any for p in posts_by_account[a] if p.t_any is not None]
        if ts:
            ref_t[a] = min(ts)
        elif anyt:
            ref_t[a] = max(anyt)
    post_times = [p.t_any for p in posts if p.t_any is not None]

    ctx = _Ctx(
        cfg=cfg,
        posts=posts,
        boosts=boosts,
        accounts=accs,
        posts_by_account=dict(posts_by_account),
        info=info,
        ref_t=ref_t,
        rng=np.random.default_rng(_seed(cfg, ordered)),
        first_t=min(post_times) if post_times else None,
        window_end=_ts(window[1]) if window else None,
        selected_by_file=selected_by_file,
    )
    report = SwarmReport(
        n_posts=len(posts),
        n_boosts=len(boosts),
        n_accounts=len(accs),
        n_accounts_known=sum(1 for a in accs if a in info),
        first_at=first_at,
        last_at=last_at,
        basis=basis,
        basis_note=basis_note,
        version=cfg.version,
        families={f: 0.0 for f in FAMILIES},
    )
    if not accs:
        report.why_unclear = "no posts of the picture"
        report.timeline = _timeline(ctx, ordered, None, False)
        return report

    sigs: list[SwarmSignal] = []
    t_sigs, p_value, burst, t_info = _timing(ctx)
    sigs += t_sigs
    sigs += _accounts(ctx, baseline)
    sigs += _copy(ctx)
    sigs += _boost(ctx)
    fam = {f: max([s.score for s in sigs if s.family == f], default=0.0) for f in FAMILIES}
    guards = _guards(ctx, fam["timing"])

    fired = [f for f in FAMILIES if fam[f] >= cfg.family_fire]
    top2 = sorted(fam.values(), reverse=True)[:2]
    score = 1 - math.prod(1 - s for s in top2)
    guard = max([g.score for g in guards], default=0.0)
    # the p-value of the timing signal that gave the family its score (clockwork has none)
    lead = max(
        (s for s in t_sigs),
        key=lambda s: (s.score, s.numbers.get("p_value") is not None, s.name),
        default=None,
    )
    lead_p = lead.numbers.get("p_value") if lead is not None else None
    timing_p_ok = not ("timing" in fired and len(fired) == cfg.min_families_swarm) or (
        lead_p is not None and lead_p <= cfg.max_p_swarm
    )
    # without a window, posts inside a few minutes say nothing about how the picture spread
    short = (
        window is None
        and t_info["timed_accounts"] >= cfg.min_burst_accounts
        and t_info["span_s"] < cfg.organic_min_span_s
    )

    if len(accs) < cfg.min_accounts:
        verdict, why = "unclear", f"only {_accounts_word(len(accs))}"
    elif (
        len(fired) >= cfg.min_families_swarm
        and score >= cfg.min_score_swarm
        and guard < cfg.guard_strong
        and timing_p_ok
    ):
        verdict, why = "swarm", ""
    elif guard >= cfg.guard_strong and not fired and not short:
        verdict, why = "organic", ""
    else:
        verdict, why = "unclear", _why_unclear(fired, guard, timing_p_ok, short, cfg)

    keep = sorted((s for s in sigs if s.score > 0), key=lambda s: (-s.score, s.family, s.name))
    keep += sorted((g for g in guards if g.score > 0), key=lambda s: (-s.score, s.family, s.name))
    burst_fired = any(s.name == "burst" and s.score > 0 for s in sigs)

    report.verdict = verdict
    report.why_unclear = why
    report.score = _r(score)
    report.guard = _r(guard)
    report.p_value = _sig(p_value) if p_value is not None else None
    report.families = {f: _r(fam[f]) for f in FAMILIES}
    report.fired = fired
    report.signals = keep
    report.burst = burst
    report.timeline = _timeline(ctx, ordered, burst, burst_fired)
    return report
