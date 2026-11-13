"""Per-cluster signals, null model, organic guards and the deterministic status rule.

Four independent families. A family's score is the max of its signals (signals inside one family
are correlated, so they're never added up). Status is decided here, by rules, not by an LLM:

  coordinated     >= min_families_coordinated families fire, p_value <= max_p_coordinated,
                  score >= min_score_coordinated, and no strong organic guard
  likely_organic  a strong organic guard and at most one family firing
  insufficient    everything else (we abstain)
"""

import hashlib
import math
import re
from dataclasses import dataclass, field

import numpy as np

from signals.config import DetectorConfig
from signals.keys import CONTENT_KINDS, Key
from signals.window import WindowData

FAMILIES = ("content", "timing", "network", "identity")


@dataclass
class AccountInfo:
    did: str
    handle: str | None = None
    display_name: str | None = None
    age_days: float | None = None  # from PLC genesis op, else profile createdAt
    followers: int | None = None
    follows: int | None = None
    posts_count: int | None = None
    description_len: int | None = None
    has_avatar: bool | None = None
    pds_host: str | None = None
    handle_changes: int | None = None


@dataclass
class Signal:
    family: str
    name: str
    score: float
    summary: str
    detail: dict = field(default_factory=dict)
    uris: list[str] = field(default_factory=list)  # post/record URIs a human can open
    dids: list[str] = field(default_factory=list)
    polarity: str = "for"  # 'for' coordination, or 'against' (organic guard / counter-evidence)


@dataclass
class ClusterScore:
    members: list[str]
    signals: list[Signal]
    family_scores: dict[str, float]
    families_fired: list[str]
    score: float
    p_value: float | None
    p_detail: dict
    guard_score: float
    status: str
    confidence: float
    post_uris: list[str]
    core: list[str]
    timeline: list[dict] = field(default_factory=list)
    member_graph: dict = field(default_factory=dict)


def _r(x: float) -> float:
    return round(float(x), 4)


def _seed(cfg: DetectorConfig, members: list[str], wd: WindowData) -> int:
    h = hashlib.blake2b(f"{cfg.seed}|{int(wd.end)}|{','.join(members)}".encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def shared_keys(members: list[str], keys: dict) -> list[Key]:
    """Keys used by >= 2 members, with uses restricted to members. Sorted for determinism."""
    mset = set(members)
    out = []
    for (kind, value), k in sorted(keys.items()):
        acc = k.accounts & mset
        if len(acc) >= 2:
            sub = Key(kind, value, [u for u in k.uses if u.did in mset], acc)
            sub.total_accounts = k.n  # type: ignore[attr-defined]
            out.append(sub)
    return out


# ---------------------------------------------------------------- content ---------------------
def content_signals(m: int, sk: list[Key], cfg: DetectorConfig) -> list[Signal]:
    out = []
    names = {
        "text": "duplicate_text",
        "neardup": "near_duplicate_text",
        "media": "duplicate_media",
        "url": "shared_link",
    }
    for kind in CONTENT_KINDS:
        cands = [k for k in sk if k.kind == kind]
        if not cands:
            continue
        best = max(cands, key=lambda k: (len(k.accounts), k.value))
        count = len(best.accounts)
        total = best.total_accounts  # type: ignore[attr-defined]
        share = count / m
        exclusivity = count / max(total, count)
        size_factor = min(1.0, count / cfg.min_cluster_size)
        score = share * exclusivity * size_factor
        what = {
            "text": "the same text",
            "neardup": "near-identical text",
            "media": "the same image/video file",
            "url": "the same link",
        }[kind]
        out.append(
            Signal(
                "content",
                names[kind],
                _r(score),
                f"{count} of {m} accounts posted {what}"
                + (
                    f" ({count} of {total} accounts using it in the window are in this cluster)"
                    if total > count
                    else ""
                ),
                {
                    "key_kind": kind,
                    "key": best.value,
                    "accounts": count,
                    "cluster_size": m,
                    "accounts_in_window": total,
                    "exclusivity": _r(exclusivity),
                    "distinct_shared_keys": len(cands),
                },
                uris=sorted({u.ref for u in best.uses})[:50],
                dids=sorted(best.accounts),
            )
        )
    return out


# ---------------------------------------------------------------- timing ----------------------
def _max_in_window(times: np.ndarray, width: float) -> np.ndarray:
    """times: (P, L) sorted along axis 1 -> (P,) max number of events within any `width` span."""
    left = times[:, :, None]
    right = times[:, None, :]
    within = (right >= left) & (right - left <= width)
    return within.sum(axis=2).max(axis=1)


def burst_stat(
    sk: list[Key],
    cfg: DetectorConfig,
    shifts: np.ndarray | None,
    member_index: dict[str, int],
    start: float,
    length: float,
) -> np.ndarray:
    """Pooled synchrony: sum over keys of (max member actions within sync_seconds) / all member
    actions on those keys. shifts: (P, n_members) circular time shifts, or None for observed."""
    num = 0.0
    den = 0.0
    p = 1 if shifts is None else shifts.shape[0]
    num = np.zeros(p)
    for k in sk:
        if len(k.uses) < cfg.min_burst_actions:
            continue
        uses = k.uses[:60]
        t = np.array([u.t - start for u in uses])
        if shifts is None:
            tt = np.sort(t)[None, :]
        else:
            idx = np.array([member_index[u.did] for u in uses])
            tt = np.sort((t[None, :] + shifts[:, idx]) % length, axis=1)
        num = num + _max_in_window(tt, cfg.sync_seconds)
        den += len(uses)
    return num / den if den else np.zeros(p)


def null_histogram(sims: np.ndarray, observed: float, bins: int = 20) -> dict:
    """Histogram of the statistic over the shifted (random) worlds, for plotting next to the
    observed value. The statistic is a share, so the bins cover [0, 1]."""
    counts, edges = np.histogram(np.clip(sims, 0, 1), bins=bins, range=(0.0, 1.0))
    return {
        "bin_edges": [_r(e) for e in edges],
        "counts": counts.astype(int).tolist(),
        "observed": _r(observed),
        "worlds": int(len(sims)),
        "worlds_at_least_observed": int((sims >= observed - 1e-12).sum()),
    }


def timing_signals(
    members: list[str], sk: list[Key], wd: WindowData, cfg: DetectorConfig, rng: np.random.Generator
) -> tuple[list[Signal], float | None, dict]:
    out: list[Signal] = []
    length = max(1.0, wd.end - wd.start)
    member_index = {d: i for i, d in enumerate(members)}
    eligible = [k for k in sk if len(k.uses) >= cfg.min_burst_actions]
    p_value = None
    p_detail: dict = {}
    if eligible:
        obs = float(burst_stat(eligible, cfg, None, member_index, wd.start, length)[0])
        # Null: shift each account's whole activity by its own random offset (circular within
        # the window). Keeps every account's own rhythm, breaks cross-account alignment.
        done, hits = 0, 0
        sims: list[np.ndarray] = []
        for batch in (100, cfg.permutations - 100):
            if batch <= 0:
                break
            shifts = rng.uniform(0, length, size=(batch, len(members)))
            sim = burst_stat(eligible, cfg, shifts, member_index, wd.start, length)
            sims.append(sim)
            hits += int((sim >= obs - 1e-12).sum())
            done += batch
            if (1 + hits) / (1 + done) > 0.1:  # clearly not significant: stop early
                break
        p_value = (1 + hits) / (1 + done)
        n_actions = sum(min(60, len(k.uses)) for k in eligible)
        # Excess synchrony over the null, mapped to [0,1]: obs fraction scaled by significance.
        score = obs * (1.0 if p_value <= cfg.max_p_coordinated else 0.5 if p_value <= 0.05 else 0)
        best = max(eligible, key=lambda k: (len(k.uses), k.value))
        bt = sorted(u.t for u in best.uses)
        p_detail = {
            "test": "circular-shift permutation of each account's activity",
            "statistic": "pooled share of member actions inside the densest "
            f"{int(cfg.sync_seconds)}s span per shared key",
            "observed": _r(obs),
            "permutations": done,
            "p_value": _r(p_value),
            "keys_tested": len(eligible),
            "actions_tested": n_actions,
            "null_distribution": null_histogram(np.concatenate(sims), obs),
        }
        out.append(
            Signal(
                "timing",
                "burst_synchrony",
                _r(score),
                f"{obs:.0%} of {n_actions} member actions on {len(eligible)} shared items fall in "
                f"tight {int(cfg.sync_seconds)}s bursts (p={p_value:.3f} vs. random time shifts)",
                {
                    **p_detail,
                    "example_key": f"{best.kind}:{best.value}",
                    "example_span_s": _r(bt[-1] - bt[0]) if bt else None,
                },
                uris=sorted({u.ref for u in best.uses})[:50],
                dids=sorted(best.accounts),
            )
        )

    # Clockwork posting: low variation of each account's inter-event intervals.
    times: dict[str, list[float]] = {d: [] for d in members}
    for p in wd.posts:
        if p.did in times:
            times[p.did].append(p.t)
    for a in wd.actions:
        if a.did in times:
            times[a.did].append(a.t)
    cvs = {}
    for d, ts in times.items():
        if len(ts) >= cfg.regularity_min_events:
            gaps = np.diff(np.sort(np.array(ts)))
            if gaps.mean() > 0:
                cvs[d] = float(gaps.std() / gaps.mean())
    if cvs:
        clockwork = sorted(d for d, cv in cvs.items() if cv <= cfg.regularity_max_cv)
        share = len(clockwork) / len(members)
        score = share * min(1.0, len(clockwork) / cfg.min_cluster_size)
        out.append(
            Signal(
                "timing",
                "clockwork_posting",
                _r(score),
                f"{len(clockwork)} of {len(members)} accounts act at near-constant intervals "
                f"(coefficient of variation <= {cfg.regularity_max_cv})",
                {
                    "accounts_with_enough_events": len(cvs),
                    "clockwork_accounts": len(clockwork),
                    "cv_by_account": {d: _r(cvs[d]) for d in sorted(cvs)[:50]},
                },
                dids=clockwork,
            )
        )
    return out, p_value, p_detail


# ---------------------------------------------------------------- network ---------------------
def network_signals(
    members: list[str], sk: list[Key], wd: WindowData, cfg: DetectorConfig
) -> list[Signal]:
    out = []
    m = len(members)
    mset = set(members)
    # Repeated co-engagement: member pairs that liked/reposted/quoted/followed >= N same items.
    eng = [k for k in sk if k.family == "engagement"]
    if eng:
        pair_counts: dict[tuple[str, str], int] = {}
        for k in eng:
            acc = sorted(k.accounts)
            for i in range(len(acc)):
                for j in range(i + 1, len(acc)):
                    pair_counts[(acc[i], acc[j])] = pair_counts.get((acc[i], acc[j]), 0) + 1
        strong = {p: c for p, c in pair_counts.items() if c >= cfg.min_engagement_shared}
        pairs = m * (m - 1) / 2
        density = len(strong) / pairs if pairs else 0
        mean_shared = float(np.mean(list(strong.values()))) if strong else 0.0
        score = density * min(1.0, mean_shared / (2 * cfg.min_engagement_shared))
        involved = sorted({d for p in strong for d in p})
        out.append(
            Signal(
                "network",
                "repeated_co_engagement",
                _r(score),
                f"{len(strong)} of {int(pairs)} account pairs engaged with the same "
                f">= {cfg.min_engagement_shared} posts/accounts (avg {mean_shared:.1f} shared)",
                {
                    "strong_pairs": len(strong),
                    "possible_pairs": int(pairs),
                    "density": _r(density),
                    "mean_shared": _r(mean_shared),
                    "shared_engagement_keys": len(eng),
                    "top_subjects": [
                        f"{k.kind}:{k.value}"
                        for k in sorted(eng, key=lambda k: (-len(k.accounts), k.value))[:10]
                    ],
                },
                uris=[
                    k.value
                    for k in sorted(eng, key=lambda k: (-len(k.accounts), k.value))
                    if k.value.startswith("at://")
                ][:20],
                dids=involved,
            )
        )
    # Reply flood: the same (near-)duplicate text posted as replies into many different threads.
    by_uri = {p.uri: p for p in wd.posts if p.did in mset}
    flood_dids, roots, flood_uris = set(), set(), set()
    for k in sk:
        if k.kind not in ("text", "neardup"):
            continue
        for u in k.uses:
            p = by_uri.get(u.ref)
            if p and p.reply_root:
                flood_dids.add(u.did)
                roots.add(p.reply_root)
                flood_uris.add(u.ref)
    if len(roots) >= 3:
        root_authors = {r.split("/")[2] for r in roots if r.startswith("at://")}
        share = len(flood_dids) / m
        out.append(
            Signal(
                "network",
                "reply_flood",
                _r(share * min(1.0, len(roots) / 5)),
                f"{len(flood_dids)} accounts replied with the same text under "
                f"{len(roots)} different threads by {len(root_authors)} authors",
                {
                    "accounts": len(flood_dids),
                    "threads": len(roots),
                    "thread_authors": len(root_authors),
                },
                uris=sorted(flood_uris)[:50],
                dids=sorted(flood_dids),
            )
        )
    # Mutual amplification: members liking/reposting each other's posts.
    amp = [
        a
        for a in wd.actions
        if a.kind in ("like", "repost")
        and a.did in mset
        and a.subject_did in mset
        and a.subject_did != a.did
    ]
    if amp:
        actors = {a.did for a in amp}
        targets = {a.subject_did for a in amp}
        both = actors & targets
        score = len(both) / m
        out.append(
            Signal(
                "network",
                "mutual_amplification",
                _r(score),
                f"{len(amp)} likes/reposts between cluster members; {len(both)} accounts both "
                "amplify and are amplified by the others",
                {
                    "actions": len(amp),
                    "actors": len(actors),
                    "targets": len(targets),
                    "both": len(both),
                },
                uris=sorted({a.subject for a in amp})[:50],
                dids=sorted(actors | targets),
            )
        )
    return out


# ---------------------------------------------------------------- identity --------------------
# Handle domains handed out to everyone: sharing one says nothing about the operator.
_SHARED_HANDLE_DOMAINS = {
    "bsky.social",
    "bsky.app",
    "brid.gy",
    "ap.brid.gy",
    "blacksky.app",
    "eurosky.social",
    "skyfeed.me",
    "bsky.team",
}

_SHAPE_RE = [(re.compile(r"[a-z]+"), "a"), (re.compile(r"[0-9]+"), "9")]


def handle_shape(handle: str) -> str:
    local, _, domain = handle.lower().partition(".")
    s = local
    for rx, rep in _SHAPE_RE:
        s = rx.sub(rep, s)
    return f"{s}.{domain}"


def _common_prefix(handles: list[str], min_len: int = 4) -> tuple[str, int]:
    counts: dict[str, int] = {}
    for h in handles:
        local = h.split(".")[0].lower()
        if len(local) >= min_len:
            counts[local[:min_len]] = counts.get(local[:min_len], 0) + 1
    if not counts:
        return "", 0
    best = max(sorted(counts), key=lambda k: counts[k])
    return best, counts[best]


def identity_signals(
    members: list[str], info: dict[str, AccountInfo], cfg: DetectorConfig
) -> list[Signal]:
    out = []
    known = [info[d] for d in members if d in info]
    if len(known) < cfg.min_cluster_size:
        return out
    aged = [a for a in known if a.age_days is not None]
    if len(aged) >= cfg.min_cluster_size:
        fresh = sorted(a.did for a in aged if a.age_days < cfg.fresh_account_days)
        ages = sorted(a.age_days for a in aged)
        out.append(
            Signal(
                "identity",
                "fresh_accounts",
                _r(len(fresh) / len(aged)),
                f"{len(fresh)} of {len(aged)} accounts are younger than "
                f"{cfg.fresh_account_days} days (median age {ages[len(ages) // 2]:.0f} days)",
                {
                    "fresh": len(fresh),
                    "with_age": len(aged),
                    "median_age_days": _r(ages[len(ages) // 2]),
                },
                dids=fresh,
            )
        )
    prof = [a for a in known if a.has_avatar is not None]
    if len(prof) >= cfg.min_cluster_size:
        empty = sorted(
            a.did
            for a in prof
            if not a.has_avatar and not a.description_len and (a.followers or 0) < 5
        )
        out.append(
            Signal(
                "identity",
                "empty_profiles",
                _r(len(empty) / len(prof)),
                f"{len(empty)} of {len(prof)} accounts have no avatar, no bio and < 5 followers",
                {"empty": len(empty), "with_profile": len(prof)},
                dids=empty,
            )
        )
    handles = [(a.did, a.handle) for a in known if a.handle and a.handle != "handle.invalid"]
    if len(handles) >= cfg.min_cluster_size:
        shapes: dict[str, list[str]] = {}
        for d, h in handles:
            shapes.setdefault(handle_shape(h), []).append(d)
        # Letters-only handles on a common domain are what most people have: not a template.
        nontrivial = {s: ds for s, ds in shapes.items() if "9" in s.split(".")[0]}
        best_shape, best = ("", [])
        if nontrivial:
            best_shape = max(sorted(nontrivial), key=lambda s: len(nontrivial[s]))
            best = nontrivial[best_shape]
        prefix, pcount = _common_prefix([h for _, h in handles])
        share = max(len(best), pcount) / len(handles)
        if share > 0:
            out.append(
                Signal(
                    "identity",
                    "templated_handles",
                    _r(share if max(len(best), pcount) >= cfg.min_cluster_size else share / 2),
                    f"{max(len(best), pcount)} of {len(handles)} handles follow one pattern"
                    + (f" ('{best_shape}')" if len(best) >= pcount else f" (prefix '{prefix}')"),
                    {
                        "shape": best_shape,
                        "shape_count": len(best),
                        "prefix": prefix,
                        "prefix_count": pcount,
                        "handles": dict(sorted(handles)[:50]),
                    },
                    dids=sorted(best)
                    if len(best) >= pcount
                    else sorted(d for d, h in handles if h.lower().startswith(prefix)),
                )
            )
    pds = [(a.did, a.pds_host) for a in known if a.pds_host]
    selfhosted = [(d, h) for d, h in pds if not h.endswith(".bsky.network")]
    if len(pds) >= cfg.min_cluster_size and selfhosted:
        counts: dict[str, list[str]] = {}
        for d, h in selfhosted:
            counts.setdefault(h, []).append(d)
        host = max(sorted(counts), key=lambda h: len(counts[h]))
        n = len(counts[host])
        out.append(
            Signal(
                "identity",
                "shared_small_pds",
                _r(n / len(pds) if n >= 2 else 0),
                f"{n} of {len(pds)} accounts are hosted on the same independent server {host}",
                {"pds_host": host, "accounts": n, "with_pds": len(pds)},
                dids=sorted(counts[host]),
            )
        )
    return out


# ---------------------------------------------------------------- organic guards --------------
def organic_guards(
    members: list[str],
    sk: list[Key],
    info: dict[str, AccountInfo],
    wd: WindowData,
    cfg: DetectorConfig,
) -> list[Signal]:
    out = []
    known = [info[d] for d in members if d in info]
    aged = [a for a in known if a.age_days is not None and a.followers is not None]
    if len(aged) >= cfg.min_cluster_size:
        est = sorted(a.did for a in aged if a.age_days > 365 and (a.followers or 0) >= 100)
        share = len(est) / len(aged)
        ages = np.array([a.age_days for a in aged])
        out.append(
            Signal(
                "guard",
                "established_accounts",
                _r(share),
                f"{len(est)} of {len(aged)} accounts are over a year old with 100+ followers "
                f"(account ages span {ages.min():.0f}-{ages.max():.0f} days)",
                {
                    "established": len(est),
                    "with_age": len(aged),
                    "age_min_days": _r(ages.min()),
                    "age_max_days": _r(ages.max()),
                },
                dids=est,
                polarity="against",
            )
        )
    if sk:
        excl = [len(k.accounts) / max(k.total_accounts, 1) for k in sk]  # type: ignore[attr-defined]
        mean_excl = float(np.mean(excl))
        out.append(
            Signal(
                "guard",
                "part_of_wider_crowd",
                _r(1 - mean_excl),
                f"on average only {mean_excl:.0%} of the accounts sharing these items "
                "in the window are in this cluster (a wider crowd is doing the same thing)",
                {"mean_exclusivity": _r(mean_excl), "shared_keys": len(sk)},
                polarity="against",
            )
        )
    handles = [
        (a.did, a.handle.lower(), a.age_days)
        for a in known
        if a.handle and a.handle != "handle.invalid"
    ]
    if len(handles) >= cfg.min_cluster_size:
        domains: dict[str, list] = {}
        for d, h, age in handles:
            parts = h.split(".")
            if len(parts) >= 3:  # sub.domain.tld: the operator's own domain
                domains.setdefault(".".join(parts[-2:]), []).append((d, age))
        common = {dom: v for dom, v in domains.items() if dom not in _SHARED_HANDLE_DOMAINS}
        if common:
            dom = max(sorted(common), key=lambda k: len(common[k]))
            group = common[dom]
            old = [d for d, age in group if age is not None and age >= 180]
            share = len(group) / len(handles)
            out.append(
                Signal(
                    "guard",
                    "transparent_automation",
                    _r(share * (len(old) / len(group)) if len(group) >= 2 else 0),
                    f"{len(group)} of {len(handles)} accounts openly share the handle domain {dom} "
                    f"({len(old)} over 6 months old): one declared operator, e.g. an automated "
                    "feed, rather than a covert campaign",
                    {"domain": dom, "accounts": len(group), "older_than_180d": len(old)},
                    dids=sorted(d for d, _ in group),
                    polarity="against",
                )
            )
    # Scheduled posting: independent bots that post "at the top of the hour" look synchronized.
    times = [u.t for k in sk if k.family == "content" for u in k.uses]
    if len(times) >= cfg.min_cluster_size:
        aligned = sum(1 for t in times if (t % 1800) < 90 or (t % 1800) > 1800 - 15)
        share = aligned / len(times)
        if share >= 0.5:
            out.append(
                Signal(
                    "guard",
                    "clock_aligned_posting",
                    _r(0.7 * share),
                    f"{aligned} of {len(times)} shared posts went out within 90 s after "
                    "a full or half hour (5% expected by chance): "
                    "scheduled bots can look synchronized without being coordinated",
                    {"aligned": aligned, "posts": len(times), "expected_share": 0.05},
                    polarity="against",
                )
            )
    langs: dict[str, int] = {}
    mset = set(members)
    for p in wd.posts:
        if p.did in mset and p.langs:
            langs[p.langs[0]] = langs.get(p.langs[0], 0) + 1
    if sum(langs.values()) >= cfg.min_cluster_size and len(langs) >= 3:
        total = sum(langs.values())
        ent = -sum((c / total) * math.log(c / total) for c in langs.values()) / math.log(len(langs))
        out.append(
            Signal(
                "guard",
                "language_diversity",
                _r(ent * min(1.0, len(langs) / 4)),
                f"members post in {len(langs)} languages",
                {"languages": dict(sorted(langs.items()))},
                polarity="against",
            )
        )
    return out


# ---------------------------------------------------------------- visuals ---------------------
# Compact shapes: DIDs are listed once in member_graph["nodes"]; timeline and edges refer to them
# by index, so a cluster's JSON stays small enough to list.
TIMELINE_MAX = 150
GRAPH_MAX_MEMBERS = 150
GRAPH_MAX_EDGES = 150


def timeline(members: list[str], sk: list[Key]) -> list[dict]:
    """Member actions on shared items, in time order: {t, n (node index), kind, key, uri}.
    `key` is a short stable id so the UI can colour actions on the same item alike."""
    index = {d: i for i, d in enumerate(members)}
    events = []
    for k in sk:
        kid = hashlib.blake2b(f"{k.kind}|{k.value}".encode(), digest_size=4).hexdigest()
        for u in k.uses:
            events.append(
                {"t": round(u.t, 3), "n": index[u.did], "kind": k.kind, "key": kid, "uri": u.ref}
            )
    events.sort(key=lambda e: (e["t"], e["n"], e["uri"]))
    if len(events) > TIMELINE_MAX:  # keep an even sample across the whole span
        idx = np.linspace(0, len(events) - 1, TIMELINE_MAX).round().astype(int)
        events = [events[i] for i in sorted(set(idx.tolist()))]
    return events


def member_graph(members: list[str], sk: list[Key]) -> dict:
    """{nodes: [did], edges: [[i, j, shared_content, shared_engagement]], truncated}: edges
    between members weighted by how many items they shared, strongest first."""
    out = {"nodes": members, "edges": [], "truncated": False}
    if len(members) > GRAPH_MAX_MEMBERS:
        out["truncated"] = True
        return out
    index = {d: i for i, d in enumerate(members)}
    pairs: dict[tuple[int, int], list[int]] = {}
    for k in sk:
        acc = sorted(index[d] for d in k.accounts)
        col = 0 if k.family == "content" else 1
        for a in range(len(acc)):
            for b in range(a + 1, len(acc)):
                pairs.setdefault((acc[a], acc[b]), [0, 0])[col] += 1
    ranked = sorted(pairs.items(), key=lambda kv: (-sum(kv[1]), kv[0]))
    out["edges"] = [[a, b, c, e] for (a, b), (c, e) in ranked[:GRAPH_MAX_EDGES]]
    out["truncated"] = len(ranked) > GRAPH_MAX_EDGES
    return out


# ---------------------------------------------------------------- combine ---------------------
def score_cluster(
    members: list[str],
    keys: dict,
    wd: WindowData,
    info: dict[str, AccountInfo],
    cfg: DetectorConfig,
) -> ClusterScore:
    rng = np.random.default_rng(_seed(cfg, members, wd))
    sk = shared_keys(members, keys)
    sigs: list[Signal] = []
    sigs += content_signals(len(members), sk, cfg)
    t_sigs, p_value, p_detail = timing_signals(members, sk, wd, cfg, rng)
    sigs += t_sigs
    sigs += network_signals(members, sk, wd, cfg)
    sigs += identity_signals(members, info, cfg)
    guards = organic_guards(members, sk, info, wd, cfg)

    fam = {f: max([s.score for s in sigs if s.family == f], default=0.0) for f in FAMILIES}
    fired = [f for f in FAMILIES if fam[f] >= cfg.family_fire]
    # Noisy-OR over the two strongest families: more families can't push it past what two show.
    top2 = sorted(fam.values(), reverse=True)[:2]
    score = 1 - math.prod(1 - s for s in top2)
    guard = max([g.score for g in guards], default=0.0)

    if (
        len(fired) >= cfg.min_families_coordinated
        and p_value is not None
        and p_value <= cfg.max_p_coordinated
        and score >= cfg.min_score_coordinated
        and guard < cfg.organic_guard_strong
    ):
        status = "coordinated"
        confidence = min(score, 1 - p_value) * (1 - guard / 2)
    elif guard >= cfg.organic_guard_strong and len(fired) <= 1:
        status = "likely_organic"
        confidence = guard * (1 - score / 2)
    else:
        status = "insufficient"
        confidence = 0.0

    post_uris = sorted({u.ref for k in sk if k.family == "content" for u in k.uses})
    involvement: dict[str, int] = {d: 0 for d in members}
    for k in sk:
        for d in k.accounts:
            involvement[d] += 1
    cutoff = np.median(list(involvement.values())) if involvement else 0
    core = sorted(d for d, c in involvement.items() if c >= cutoff and c > 0)
    return ClusterScore(
        members=members,
        signals=sigs + guards,
        family_scores={f: _r(v) for f, v in fam.items()},
        families_fired=fired,
        score=_r(score),
        p_value=_r(p_value) if p_value is not None else None,
        p_detail=p_detail,
        guard_score=_r(guard),
        status=status,
        confidence=_r(confidence),
        post_uris=post_uris,
        core=core,
        timeline=timeline(members, sk),
        member_graph=member_graph(members, sk),
    )
