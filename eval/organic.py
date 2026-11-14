"""False-positive eval: how often the detector calls ORGANIC activity `coordinated`.

Two datasets, reported separately (rows in `eval_runs`, split `heldout` by default):

  organic_synth  synthetic hard negatives from `signals.synth.hard_negative` (viral, news_link,
                 live_event, fandom), injected one at a time IN MEMORY into real recorded
                 10-minute windows. Labels are true by construction, but the cases are synthetic.
  organic_live   real organic-looking bursts in the recorded Jetstream data, picked by
                 `select_live_cases` (a fixed rule on raw data + public profiles that never
                 looks at detector output). The labels are HEURISTIC, not ground truth, until a
                 human checks a sample. The selected list is saved in
                 `eval/fixtures/organic_live.json` so it can be reviewed.

The detector runs through its own entry point (`signals.detect.detect`) on a `WindowData` built
in memory from recorded Jetstream files with the ingest normaliser, mirroring
`signals.window.load_window` (no database). A case's outcome is the "worst" status among the
clusters that contain >= 2 of its accounts: coordinated (a false positive) > likely_organic >
insufficient (abstained) > no_cluster (never grouped, so never shown).

    uv run python -m eval.organic select   # pick live cases -> eval/fixtures/organic_live.json
    uv run python -m eval.organic run      # both datasets; prints a report, writes nothing
    uv run python -m eval.organic run --write --database-url postgresql://...   # -> eval_runs
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from eval.recall import _git_sha, load_base_rates  # shared with the recall eval
from ingest.normalize import Action, Delete, Post, normalize
from ingest.recorder import read_events, recorded_files
from signals.baseline import BaseRates
from signals.config import DEFAULT, DetectorConfig
from signals.detect import detect
from signals.score import AccountInfo
from signals.synth import SYNTH_PREFIX, Synthetic, hard_negative, inject
from signals.textnorm import normalize_text
from signals.window import WAction, WindowData, WPost

log = logging.getLogger("eval.organic")

REPO = Path(__file__).resolve().parent.parent
SLICES = Path(os.environ.get("EVAL_SLICES_DIR", "/srv/junction-espoo/data/eval-slices"))
FIXTURE_LIVE = REPO / "eval" / "fixtures" / "organic_live.json"
WINDOW_S = 600  # the detector's main window (10m), aligned to the UTC clock
SYNTH_KINDS = ("viral", "news_link", "live_event", "fandom")
SEEDS = 3  # synthetic seeds per kind and window in a published run
ENRICH_BUDGET = 400  # accounts per detector pass, as signals/ enriches in production
OUTCOMES = ("coordinated", "likely_organic", "insufficient", "no_cluster")


# ------------------------------------------------------------------ split ---------------------
def split_of(window_start: float) -> str:
    """Held-out rule: a window belongs to the UTC hour it starts in; odd hours are `heldout`,
    even hours are `dev` (tuning). Whole hours, so neighbouring windows of one burst stay on
    one side."""
    return "heldout" if datetime.fromtimestamp(window_start, UTC).hour % 2 else "dev"


# ------------------------------------------------------------------ windows from recordings ---
@dataclass
class HourData:
    """Normalised rows of one recorded slice, bucketed into clock-aligned 10m windows."""

    posts: dict[int, dict[str, WPost]] = field(default_factory=lambda: defaultdict(dict))
    # window -> table -> uri -> (did, t, subject, subject_did)
    actions: dict[int, dict[str, dict[str, tuple]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(dict))
    )
    tags: dict[str, list[str]] = field(default_factory=dict)  # post uri -> hashtags
    deletes: dict[str, float] = field(default_factory=dict)  # uri -> first delete time
    seconds: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))

    def complete_windows(self, min_covered: float = 0.98) -> list[int]:
        """Windows the recording covers: at least 98% of their seconds have events (the stream
        is hundreds of events/s, so an empty second means a gap, e.g. a recorder restart)."""
        return sorted(w for w, secs in self.seconds.items() if len(secs) >= min_covered * WINDOW_S)


def read_rows(paths: Iterable[Path]) -> HourData:
    data = HourData()
    for _raw, event in read_events(list(paths)):
        row = normalize(event)
        if row is None:
            continue
        if not isinstance(row, Post | Action | Delete):
            continue
        t = row.seen_at.timestamp()
        if isinstance(row, Delete):
            data.deletes.setdefault(row.uri, t)
            continue
        w = int(t // WINDOW_S) * WINDOW_S
        data.seconds[w].add(int(t))
        if isinstance(row, Post):
            if row.uri not in data.posts[w]:
                data.posts[w][row.uri] = WPost(
                    uri=row.uri,
                    did=row.did,
                    t=t,
                    text=row.text,
                    text_hash=row.text_hash,
                    embed_urls=row.embed_urls,
                    media_hashes=row.media_hashes,
                    reply_to=row.reply_to,
                    reply_root=row.reply_root,
                    quote_uri=row.quote_uri,
                    created_t=row.created_at.timestamp() if row.created_at else None,
                    langs=row.langs or [],
                )
                if row.tags:
                    data.tags[row.uri] = row.tags
        else:
            subject = row.subject_uri if row.table != "follows" else row.subject_did
            data.actions[w][row.table].setdefault(row.uri, (row.did, t, subject, row.subject_did))
    return data


def build_window(data: HourData, w: int, cfg: DetectorConfig = DEFAULT) -> WindowData:
    """WindowData for [w, w + 600), the same selection `signals.window.load_window` makes in SQL:
    every post; likes/reposts/follows whose subject was hit by min..max_key_accounts distinct
    accounts in the window; per-second background counts of every row; distinct posting accounts.
    Difference: `deleted` only reflects deletes recorded before the window ends (the live
    detector can't see later ones either)."""
    end = w + WINDOW_S
    wd = WindowData(start=float(w), end=float(end))
    for uri in sorted(data.posts.get(w, {})):
        p = data.posts[w][uri]
        p.deleted = data.deletes.get(uri, float("inf")) < end
        wd.posts.append(p)
    times: dict[str, list[float]] = {"post": [p.t for p in wd.posts]}
    tables = data.actions.get(w, {})
    for table, kind in (("likes", "like"), ("reposts", "repost"), ("follows", "follow")):
        rows = tables.get(table, {})
        times[kind] = [r[1] for r in rows.values()]
        by_subject: dict[str, set[str]] = defaultdict(set)
        for did, _t, subject, _sd in rows.values():
            by_subject[subject].add(did)
        keep = {
            s
            for s, d in by_subject.items()
            if cfg.min_key_accounts <= len(d) <= cfg.max_key_accounts
        }
        for uri in sorted(rows):
            did, t, subject, subject_did = rows[uri]
            if subject in keep:
                deleted = data.deletes.get(uri, float("inf")) < end
                wd.actions.append(WAction(kind, uri, did, t, subject, subject_did, deleted))
    wd.background = WindowData.background_from_times(wd.start, wd.end, times)
    wd.active_accounts = len({p.did for p in wd.posts})
    return wd


def slice_hours(root: Path) -> dict[str, list[Path]]:
    """Recorded files grouped by UTC hour ('YYYYMMDDHH'); several files per hour are possible
    (one per recorder process)."""
    hours: dict[str, list[Path]] = defaultdict(list)
    for p in recorded_files(root):
        hours[p.parent.name + p.name[:2]].append(p)
    return dict(sorted(hours.items()))


def hour_split(hour: str) -> str:
    return split_of(datetime.strptime(hour, "%Y%m%d%H").replace(tzinfo=UTC).timestamp())


# ------------------------------------------------------------------ account profiles ---------
class Profiles:
    """Public profile + PLC data per DID, fetched with `signals.enrich`'s read-only fetchers and
    cached in a JSON file next to the slices, so a re-run sees the same numbers. Only successful
    lookups are cached; a failed or empty lookup is remembered for this run only (`attempted`),
    so a transient error never turns into a permanently "missing" account."""

    def __init__(self, path: Path | None, fetch: bool = True) -> None:
        self.path = path
        self.fetch = fetch
        self.data: dict[str, dict] = {}
        if path and path.exists():
            for d, e in json.loads(path.read_text()).items():
                # older versions cached failures as {"missing": true, "fetched_at": ...} and
                # plc None: drop them so the lookup is retried (keep a successful PLC part)
                if e.get("missing"):
                    e = {"plc": e["plc"]} if e.get("plc") else {}
                e = {k: v for k, v in e.items() if not (k == "plc" and v is None)}
                if e:
                    self.data[d] = e
        self.fetched = 0
        self.dirty: set[str] = set()  # DIDs added or changed since the last reset
        self.attempted: set[str] = set()  # looked up in this run (whether or not it worked)

    def save(self) -> None:
        if self.path and self.fetched:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
            tmp.replace(self.path)

    def merge(self, new: dict[str, dict]) -> None:
        """Merge successful lookups made by worker processes."""
        for d, e in new.items():
            self.data[d] = {**self.data.get(d, {}), **e}

    def ensure(self, dids: list[str], plc: bool = True) -> None:
        """Look up profiles (and PLC logs) that aren't cached or tried yet in this run."""
        real = [
            d
            for d in dict.fromkeys(dids)
            if not d.startswith(SYNTH_PREFIX) and d not in self.attempted
        ]
        need_profile = [d for d in real if "fetched_at" not in self.data.get(d, {})]
        need_plc = [
            d
            for d in real
            if plc and d.startswith("did:plc:") and "plc" not in self.data.get(d, {})
        ]
        if not self.fetch or not (need_profile or need_plc):
            return
        import httpx

        from signals.enrich import UA, fetch_plc, fetch_profiles

        now = datetime.now(UTC).isoformat()
        with httpx.Client(headers=UA, follow_redirects=True) as client:
            profiles = fetch_profiles(client, need_profile) if need_profile else {}
            for d in need_profile:
                if p := profiles.get(d):
                    self.data.setdefault(d, {}).update(
                        handle=p.get("handle"),
                        display_name=(p.get("displayName") or "")[:200],
                        created=p.get("createdAt"),
                        followers=p.get("followersCount"),
                        follows=p.get("followsCount"),
                        posts=p.get("postsCount"),
                        desc=len(p.get("description") or ""),
                        avatar=bool(p.get("avatar")),
                        fetched_at=now,
                    )
                    self.dirty.add(d)
            if need_plc:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=8) as pool:
                    got = pool.map(lambda d: fetch_plc(client, d), need_plc)
                    for d, q in zip(need_plc, got, strict=True):
                        if q:
                            self.data.setdefault(d, {})["plc"] = {
                                "created": q["created"].isoformat() if q["created"] else None,
                                "handle_changes": q["handle_changes"],
                                "pds_host": q["pds_host"],
                            }
                            self.dirty.add(d)
        if plc:  # a profile-only lookup doesn't count as a full attempt
            self.attempted.update(real)
        self.fetched += len(need_profile) + len(need_plc)

    def info(self, did: str, at: float) -> AccountInfo | None:
        """AccountInfo as `signals.accounts.load_account_info` builds it, with the age measured
        at `at` (the window end) instead of now. Like production, an account whose lookup was
        tried but failed gets an AccountInfo with empty fields; one never tried gets None."""
        e = self.data.get(did)
        if not e:
            return AccountInfo(did=did) if did in self.attempted else None
        plc = e.get("plc") or {}
        borns = [_ts(x) for x in (plc.get("created"), e.get("created"))]
        borns = [b for b in borns if b is not None]
        born = min(borns) if borns else None
        return AccountInfo(
            did=did,
            handle=e.get("handle"),
            display_name=e.get("display_name"),
            age_days=(at - born) / 86400 if born is not None else None,
            followers=e.get("followers"),
            follows=e.get("follows"),
            posts_count=e.get("posts"),
            description_len=e.get("desc"),
            has_avatar=e.get("avatar"),
            pds_host=plc.get("pds_host"),
            handle_changes=plc.get("handle_changes"),
        )


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def account_info_fn(
    profiles: Profiles, at: float, synth: Synthetic | None = None, budget: int = ENRICH_BUDGET
) -> Callable[[list[str]], dict[str, AccountInfo]]:
    """The detector's `account_info` callback: one production enrichment pass. The first
    `budget` real accounts of the detector's priority list (strongest suspicious cluster first)
    are looked up, and only they get info, whatever else the cache holds, so every pass sees a
    fixed view and results don't depend on job order, worker count or a warm cache. Production
    re-runs a window every minute and can accumulate more; this is one pass. Synthetic accounts
    always come with their generated info."""

    def fn(dids: list[str]) -> dict[str, AccountInfo]:
        out: dict[str, AccountInfo] = {}
        if synth:
            out.update({d: synth.accounts[d] for d in dids if d in synth.accounts})
        real = [d for d in dids if not d.startswith(SYNTH_PREFIX)][:budget]
        profiles.ensure(real)
        for d in real:
            if (a := profiles.info(d, at)) is not None:
                out[d] = a
        return out

    return fn


# ------------------------------------------------------------------ live organic cases -------
# The rule only reads raw window data and public profiles, never detector output. Thresholds are
# fixed here and recorded in the fixture; change LIVE_RULE_VERSION when one changes.
LIVE_RULE_VERSION = "live-rule-v1"
LIVE_RULE = {
    "link_min_accounts": 5,  # distinct accounts posting the same normalised URL in one window
    "tag_min_accounts": 8,  # distinct accounts using the same hashtag in one window
    "post_min_engagers": 50,  # distinct accounts liking/reposting one post in one window
    "post_max_cases_per_window": 10,  # largest popular posts only (keeps profile lookups sane)
    "max_posts_per_account": 3,  # one account repeating the key more than this = not a crowd
    "max_duplicate_text_share": 0.2,  # share of posts whose normalised text another account used
    "established_min_age_days": 365,
    "established_min_followers": 30,
    "established_min_posts": 30,
    "min_established_share": 0.8,  # of the accounts whose profile resolved
    "min_resolved_share": 0.8,
    "profile_sample": 40,  # accounts checked per case (deterministic sample by DID hash)
}


def _sample(dids: Iterable[str], k: int) -> list[str]:
    return sorted(dids, key=lambda d: hashlib.blake2b(d.encode(), digest_size=8).digest())[:k]


def case_accounts(data: HourData, w: int, ctype: str, key: str) -> tuple[set[str], list[str]]:
    """(accounts, post or engagement URIs) of a live case: everyone who did the thing."""
    if ctype in ("link_burst", "hashtag_burst"):
        posts = [
            p
            for p in data.posts.get(w, {}).values()
            if (key in p.embed_urls if ctype == "link_burst" else key in data.tags.get(p.uri, []))
        ]
        return {p.did for p in posts}, sorted(p.uri for p in posts)
    if ctype == "popular_post":
        rows = [
            (uri, r[0])
            for table in ("likes", "reposts")
            for uri, r in data.actions.get(w, {}).get(table, {}).items()
            if r[2] == key
        ]
        return {d for _u, d in rows}, sorted(u for u, _d in rows)
    raise ValueError(ctype)


def _post_url(uri: str) -> str:
    did, _col, rkey = uri[5:].split("/", 2)
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def _established(profiles: Profiles, dids: list[str], at: float, rule: dict) -> tuple[float, float]:
    """(share resolved, share of resolved that are established)."""
    resolved, est = 0, 0
    for d in dids:
        a = profiles.info(d, at)
        if a is None or a.age_days is None:
            continue
        resolved += 1
        est += (
            a.age_days >= rule["established_min_age_days"]
            and (a.followers or 0) >= rule["established_min_followers"]
            and (a.posts_count or 0) >= rule["established_min_posts"]
        )
    return (resolved / len(dids) if dids else 0.0), (est / resolved if resolved else 0.0)


def candidate_keys(data: HourData, w: int, rule: dict = LIVE_RULE) -> list[dict]:
    """Step 1 of the rule (raw data only): popular links, hashtags and posts in window w whose
    posts come from many accounts with diverse text."""
    posts = list(data.posts.get(w, {}).values())
    out = []
    for ctype, keys_of, min_acc in (
        ("link_burst", lambda p: set(p.embed_urls), rule["link_min_accounts"]),
        ("hashtag_burst", lambda p: set(data.tags.get(p.uri, [])), rule["tag_min_accounts"]),
    ):
        by_key: dict[str, list[WPost]] = defaultdict(list)
        for p in posts:
            for k in keys_of(p):
                by_key[k].append(p)
        for k, ps in by_key.items():
            per_acc = Counter(p.did for p in ps)
            if len(per_acc) < min_acc or max(per_acc.values()) > rule["max_posts_per_account"]:
                continue
            # posts with no text (bare link cards) say nothing about diversity: left out
            texts = [(p.did, t) for p in ps if (t := normalize_text(p.text or ""))]
            if len({d for d, _t in texts}) < min_acc:
                continue
            accs_by_text: dict[str, set[str]] = defaultdict(set)
            for d, t in texts:
                accs_by_text[t].add(d)
            dup = sum(1 for _d, t in texts if len(accs_by_text[t]) > 1) / len(texts)
            if dup > rule["max_duplicate_text_share"]:
                continue
            out.append(
                {
                    "type": ctype,
                    "key": k,
                    "accounts": len(per_acc),
                    "posts": len(ps),
                    "duplicate_text_share": round(dup, 3),
                }
            )
    engagers: dict[str, set[str]] = defaultdict(set)
    for table in ("likes", "reposts"):
        for did, _t, subject, _sd in data.actions.get(w, {}).get(table, {}).values():
            engagers[subject].add(did)
    popular = sorted(
        ((s, d) for s, d in engagers.items() if len(d) >= rule["post_min_engagers"]),
        key=lambda x: (-len(x[1]), x[0]),
    )[: rule["post_max_cases_per_window"]]
    for s, d in popular:
        out.append({"type": "popular_post", "key": s, "accounts": len(d), "posts": len(d)})
    return sorted(out, key=lambda c: (c["type"], -c["accounts"], c["key"]))


def select_live_cases(
    data: HourData, w: int, profiles: Profiles, rule: dict = LIVE_RULE
) -> list[dict]:
    """Step 2 of the rule: keep candidates whose accounts are mostly long-established."""
    at = float(w + WINDOW_S)
    cands = candidate_keys(data, w, rule)
    samples = {}
    for c in cands:
        dids, _uris = case_accounts(data, w, c["type"], c["key"])
        samples[(c["type"], c["key"])] = _sample(dids, rule["profile_sample"])
    profiles.ensure([d for s in samples.values() for d in s], plc=False)
    # accounts with no profile createdAt: their PLC genesis date decides the age
    profiles.ensure(
        [d for s in samples.values() for d in s if not (profiles.data.get(d) or {}).get("created")]
    )
    cases = []
    stamp = datetime.fromtimestamp(w, UTC).strftime("%Y%m%dT%H%M")
    for c in cands:
        sample = samples[(c["type"], c["key"])]
        resolved, est = _established(profiles, sample, at, rule)
        if resolved < rule["min_resolved_share"] or est < rule["min_established_share"]:
            continue
        _dids, uris = case_accounts(data, w, c["type"], c["key"])
        kid = hashlib.blake2b(c["key"].encode(), digest_size=4).hexdigest()
        cases.append(
            {
                "id": f"{c['type']}:{stamp}:{kid}",
                **c,
                "window_start": datetime.fromtimestamp(w, UTC).isoformat(),
                "split": split_of(w),
                "checked_accounts": len(sample),
                "resolved_share": round(resolved, 3),
                "established_share": round(est, 3),
                "why": _why(c, est, len(sample)),
                "sample_urls": [_post_url(u) for u in uris[:5]]
                if c["type"] != "popular_post"
                else [_post_url(c["key"])],
            }
        )
    return cases


def _why(c: dict, est: float, checked: int) -> str:
    what = {
        "link_burst": "posted the same link",
        "hashtag_burst": "used the same hashtag",
        "popular_post": "liked or reposted the same post",
    }[c["type"]]
    extra = (
        f", {c['duplicate_text_share']:.0%} of posts reuse another account's text"
        if "duplicate_text_share" in c
        else ""
    )
    return (
        f"{c['accounts']} accounts {what} within 10 minutes{extra}; {est:.0%} of {checked} "
        "checked accounts are 1+ year old with 30+ followers and 30+ posts"
    )


# ------------------------------------------------------------------ scoring ------------------
@dataclass
class CaseResult:
    case_id: str
    dataset: str  # organic_synth | organic_live
    case_type: str
    window_start: float
    accounts: int
    outcome: str  # one of OUTCOMES
    clusters: list[dict] = field(default_factory=list)  # clusters holding >= 2 case accounts


def case_outcome(
    clusters: list, dids: set[str], min_overlap: int = 2, min_share: float = 0.5
) -> tuple[str, list[dict]]:
    """Worst status among the clusters that are about this case: >= min_overlap of the case's
    accounts AND they make up >= min_share of the cluster (two likers of a viral post who also
    sit in an unrelated ring don't make that ring the viral post's verdict)."""
    hits = []
    for c in clusters:
        overlap = len(dids.intersection(c.members))
        if overlap < min_overlap or overlap < min_share * len(c.members):
            continue
        hits.append(
            {
                "status": c.status,
                "size": len(c.members),
                "overlap": overlap,
                "score": round(float(c.score), 3),
                "p_value": None if c.p_value is None else round(float(c.p_value), 4),
                "guard": round(float(c.guard_score), 3),
                "families_fired": list(c.families_fired),
                "signals_for": sorted(
                    s.name for s in c.signals if s.polarity == "for" and s.score >= 0.5
                ),
                "guards": sorted(s.name for s in c.signals if s.polarity == "against"),
            }
        )
    statuses = {h["status"] for h in hits}
    outcome = next(
        (o for o in ("coordinated", "likely_organic", "insufficient") if o in statuses),
        "no_cluster",
    )
    hits.sort(key=lambda h: (OUTCOMES.index(h["status"]), -h["overlap"]))
    return outcome, hits


def summarize(results: list[CaseResult]) -> dict:
    """FPR = share of organic cases that came out `coordinated`, with every outcome counted."""
    n = len(results)
    counts = Counter(r.outcome for r in results)
    clustered = n - counts.get("no_cluster", 0)
    by_type: dict[str, dict] = {}
    for t in sorted({r.case_type for r in results}):
        rs = [r for r in results if r.case_type == t]
        c = Counter(r.outcome for r in rs)
        by_type[t] = {
            "n": len(rs),
            **{o: c.get(o, 0) for o in OUTCOMES},
            "fpr": round(c.get("coordinated", 0) / len(rs), 4),
        }
    fps = [
        {
            "case_id": r.case_id,
            "type": r.case_type,
            "window_start": datetime.fromtimestamp(r.window_start, UTC).isoformat(),
            "accounts": r.accounts,
            "clusters": [h for h in r.clusters if h["status"] == "coordinated"],
            "families_fired": sorted(
                {f for h in r.clusters if h["status"] == "coordinated" for f in h["families_fired"]}
            ),
        }
        for r in results
        if r.outcome == "coordinated"
    ]
    return {
        "n": n,
        "fpr": round(counts.get("coordinated", 0) / n, 4) if n else None,
        "fpr_ci95": _wilson(counts.get("coordinated", 0), n),
        "counts": {o: counts.get(o, 0) for o in OUTCOMES},
        # The harder number: among cases the detector did group into a cluster, how many it
        # called coordinated (no_cluster cases are easy wins that inflate n).
        "clustered": clustered,
        "fpr_given_clustered": round(counts.get("coordinated", 0) / clustered, 4)
        if clustered
        else None,
        "by_type": by_type,
        "false_positives": fps,
    }


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(float(max(0.0, c - h)), 4), round(float(min(1.0, c + h)), 4)]


# ------------------------------------------------------------------ runner -------------------
# Worker processes are forked after one hour of recordings is loaded; they read it from here.
_G: dict = {}


def _job(w: int, kind: str | None, seed: int | None, live: list[dict]) -> tuple:
    """One detector pass on window w: the plain window (live cases) or with one synthetic hard
    negative injected. Returns (CaseResults, detector stats, new lookups, DIDs tried)."""
    data: HourData = _G["data"]
    profiles: Profiles = _G["profiles"]
    cfg: DetectorConfig = _G["cfg"]
    profiles.dirty.clear()
    base = build_window(data, w, cfg)
    at = float(w + WINDOW_S)
    results = []
    if kind is None:
        res = detect(
            base, account_info_fn(profiles, at, budget=_G["budget"]), cfg, base_rates=_G["rates"]
        )
        for c in live:
            dids, _uris = case_accounts(data, w, c["type"], c["key"])
            if len(dids) != c["accounts"]:
                log.warning("%s: %d accounts now, %d in fixture", c["id"], len(dids), c["accounts"])
            outcome, hits = case_outcome(res.clusters, dids)
            results.append(
                CaseResult(c["id"], "organic_live", c["type"], w, len(dids), outcome, hits)
            )
    else:
        s = hard_negative(kind, base, seed=seed)
        info = account_info_fn(profiles, at, s, _G["budget"])
        res = detect(inject(base, s), info, cfg, base_rates=_G["rates"])
        outcome, hits = case_outcome(res.clusters, s.dids)
        stamp = datetime.fromtimestamp(w, UTC).strftime("%Y%m%dT%H%M")
        results.append(
            CaseResult(
                f"{kind}:{stamp}:s{seed}", "organic_synth", kind, w, len(s.dids), outcome, hits
            )
        )
    new = {d: profiles.data[d] for d in profiles.dirty}
    attempted = set(profiles.attempted)
    stats = {"window_start": w, "job": kind or "live", "seed": seed, **res.stats}
    stats.pop("seconds", None)
    return results, stats, new, attempted


def run_eval(
    slices: Path,
    split: str,
    profiles: Profiles,
    live_cases: list[dict],
    seeds: list[int],
    kinds: Iterable[str] = SYNTH_KINDS,
    cfg: DetectorConfig = DEFAULT,
    workers: int = 4,
    budget: int = ENRICH_BUDGET,
    max_windows: int | None = None,
    base_rates: BaseRates | None = None,
) -> tuple[list[CaseResult], list[dict], list[str]]:
    """Run every job on the complete windows of the requested split. Returns (results, per-pass
    detector stats, slice files used). `base_rates` turns on the identity base-rate test, as
    the live worker runs it (None: off)."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    results: list[CaseResult] = []
    stats: list[dict] = []
    files: list[str] = []
    done = 0
    for hour, paths in slice_hours(slices).items():
        if hour_split(hour) != split or (max_windows is not None and done >= max_windows):
            continue
        t0 = time.monotonic()
        data = read_rows(paths)
        windows = data.complete_windows()
        if max_windows is not None:
            windows = windows[: max_windows - done]
        done += len(windows)
        if not windows:
            continue
        files += [str(p) for p in paths]
        log.info("hour %s: %d windows (read in %.0fs)", hour, len(windows), time.monotonic() - t0)
        live_jobs, synth_jobs = [], []
        for w in windows:
            ws = datetime.fromtimestamp(w, UTC).isoformat()
            live_jobs.append((w, None, None, [c for c in live_cases if c["window_start"] == ws]))
            synth_jobs += [(w, k, s, []) for k in kinds for s in seeds]
        # Live passes first, then the synthetic ones, so the forked workers of the second stage
        # start with the accounts the first stage looked up instead of all fetching them again.
        for jobs in (live_jobs, synth_jobs):
            _G.update(data=data, profiles=profiles, cfg=cfg, budget=budget, rates=base_rates)
            if workers > 1:
                with ProcessPoolExecutor(workers, mp_context=get_context("fork")) as pool:
                    outs = list(pool.map(_job, *zip(*jobs, strict=True)))
            else:
                outs = [_job(*j) for j in jobs]
            for rs, st, new, attempted in outs:
                results += rs
                stats.append(st)
                profiles.merge(new)
                profiles.attempted |= attempted
                profiles.fetched += len(new)
        profiles.save()
        _G.clear()
    return results, stats, files


def build_details(dataset: str, summary: dict, stats: list[dict], files: list[str], extra: dict):
    labels = {
        "organic_synth": "synthetic hard negatives from signals.synth (labels true by "
        "construction), injected one at a time into real recorded windows",
        "organic_live": "real recorded bursts picked by a fixed heuristic rule; labels are "
        "heuristic, not ground truth, until a human checks a sample",
    }
    return {
        "metric": "fpr = share of organic cases the detector marked coordinated (Shill)",
        "label_source": labels[dataset],
        "detector_version": DEFAULT.version,
        **summary,
        "outcome_rule": "worst status among clusters where the case's accounts are >= 2 and "
        ">= 50% of the members; "
        "no_cluster = never grouped (not shown)",
        "windows": sorted(
            {datetime.fromtimestamp(s["window_start"], UTC).isoformat() for s in stats}
        ),
        "window_seconds": WINDOW_S,
        "split_rule": "UTC hour of window start: odd = heldout, even = dev",
        "slice_files": [Path(f).name for f in files],
        **extra,
    }


def report(dataset: str, split: str, summary: dict) -> str:
    lines = [
        f"{dataset} [{split}] n={summary['n']}  FPR={summary['fpr']}  "
        f"95% CI={summary['fpr_ci95']}  counts={summary['counts']}  "
        f"FPR among clustered={summary['fpr_given_clustered']} (n={summary['clustered']})"
    ]
    for t, b in summary["by_type"].items():
        lines.append(
            f"  {t:<14} n={b['n']:<4} coordinated={b['coordinated']:<3} "
            f"likely_organic={b['likely_organic']:<3} insufficient={b['insufficient']:<3} "
            f"no_cluster={b['no_cluster']:<3} fpr={b['fpr']}"
        )
    for fp in summary["false_positives"]:
        c = fp["clusters"][0]
        lines.append(
            f"  FP {fp['case_id']}: cluster of {c['size']} ({c['overlap']} case accounts), "
            f"families={c['families_fired']}, signals={c['signals_for']}, score={c['score']}, "
            f"p={c['p_value']}, guard={c['guard']} {c['guards']}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI ----------------------
def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("select", "run"):
        q = sub.add_parser(name)
        q.add_argument("--slices", type=Path, default=SLICES / "jetstream")
        q.add_argument("--profiles", type=Path, default=SLICES / "accounts" / "profiles.json")
        q.add_argument("--split", choices=["heldout", "dev"], default="heldout")
        q.add_argument("--fixture", type=Path, default=FIXTURE_LIVE)
        q.add_argument("--offline", action="store_true", help="no profile fetching")
        q.add_argument("--max-windows", type=int)
    r = sub.choices["run"]
    r.add_argument("--seeds", type=int, default=SEEDS, help="synthetic seeds per kind and window")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--budget", type=int, default=ENRICH_BUDGET)
    r.add_argument("--json", type=Path, help="also write both eval rows as JSON here")
    r.add_argument("--write", action="store_true", help="insert into eval_runs")
    r.add_argument("--database-url", help="default: DATABASE_URL from the environment / .env")
    r.add_argument(
        "--base-rates",
        help="JSON with measured base rates (turns on the identity base-rate test, as live)",
    )


def main_args(a: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    profiles = Profiles(a.profiles, fetch=not a.offline)
    if a.action == "select":
        cases, files, n_windows = [], [], 0
        for hour, paths in slice_hours(a.slices).items():
            if hour_split(hour) != a.split:
                continue
            left = None if a.max_windows is None else a.max_windows - n_windows
            if left is not None and left <= 0:
                break
            data = read_rows(paths)
            windows = data.complete_windows()[:left]
            n_windows += len(windows)
            files += [p.name for p in paths] if windows else []
            for w in windows:
                cases += select_live_cases(data, w, profiles)
                log.info("%s: %d cases so far", datetime.fromtimestamp(w, UTC), len(cases))
            profiles.save()
        fixture = {
            "rule_version": LIVE_RULE_VERSION,
            "rule": LIVE_RULE,
            "rule_text": LIVE_RULE_TEXT,
            "labels": "HEURISTIC organic labels, not ground truth, until a human checks a sample",
            "split": a.split,
            "window_seconds": WINDOW_S,
            "slice_files": files,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "cases": cases,
        }
        if not cases:
            print(f"no live cases found in {a.slices} [{a.split}]; {a.fixture} left as it was")
            return 1
        a.fixture.parent.mkdir(parents=True, exist_ok=True)
        a.fixture.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
        print(f"{len(cases)} live cases -> {a.fixture}  {dict(Counter(c['type'] for c in cases))}")
        return 0

    adhoc = a.max_windows is not None or a.seeds != SEEDS or a.budget != ENRICH_BUDGET
    if a.write and adhoc:
        # an ad-hoc run is not the split's result: never publish it as one (same rule as recall)
        print("--max-windows, --seeds, --budget: ad-hoc runs, never --write", file=sys.stderr)
        return 2
    if not a.fixture.exists():
        print(f"no live-case fixture at {a.fixture}: run `python -m eval organic select` first")
        return 1
    fixture = json.loads(a.fixture.read_text())
    commit = _git_sha()  # at start: the tree may change while a long run goes
    rates = load_base_rates(a.base_rates) if a.base_rates else None
    live = [c for c in fixture["cases"] if c["split"] == a.split]
    seeds = list(range(1000, 1000 + a.seeds)) if a.split == "heldout" else list(range(a.seeds))
    results, stats, files = run_eval(
        a.slices,
        a.split,
        profiles,
        live,
        seeds,
        workers=a.workers,
        budget=a.budget,
        max_windows=a.max_windows,
        base_rates=rates,
    )
    # A fixture case whose window is no longer complete on disk (pruned, cut by --max-windows)
    # is not scored; say so instead of reporting the FPR on fewer cases.
    scored = {r.case_id for r in results if r.dataset == "organic_live"}
    missing = sorted(c["id"] for c in live if c["id"] not in scored)
    if missing:
        print(f"warning: {len(missing)} of {len(live)} live cases have no complete window")
    rows = []
    for dataset, extra in (
        ("organic_synth", {"kinds": list(SYNTH_KINDS), "seeds": seeds}),
        (
            "organic_live",
            {
                "fixture": str(a.fixture.relative_to(REPO))
                if a.fixture.is_relative_to(REPO)
                else str(a.fixture),
                "rule_version": fixture.get("rule_version"),
                "fixture_cases": len(live),
                "missing_cases": missing,
            },
        ),
    ):
        rs = [r for r in results if r.dataset == dataset]
        summary = summarize(rs)
        details = build_details(dataset, summary, stats, files, extra)
        details.update(
            eval_commit=commit,
            enrich_budget=a.budget,
            # None: the identity base-rate test was off (the live worker measures base rates)
            base_rates={**rates.as_dict(), "source": a.base_rates} if rates else None,
        )
        rows.append(
            {
                "dataset": dataset,
                "split": a.split,
                "fpr": summary["fpr"],
                "n": summary["n"],
                "details": details,
            }
        )
        print(report(dataset, a.split, summary))
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1) + "\n")
    if a.write:
        from eval.store import write_eval_run

        for row in rows:
            if not row["n"]:
                print(f"skip {row['dataset']}: n=0")
                continue
            rid = write_eval_run(database_url=a.database_url, **row)
            print(f"eval_runs id={rid} {row['dataset']} [{row['split']}] written")
    return 0


LIVE_RULE_TEXT = (
    "In each complete 10-minute window of the split: (a) a normalised link posted by >= 5 "
    "distinct accounts, or (b) a hashtag used by >= 8 distinct accounts, where no account posts "
    "it more than 3 times, at least that many accounts wrote some text, and <= 20% of the posts "
    "with text reuse another account's normalised text; or "
    "(c) one of the 10 posts liked/reposted by the most (>= 50) distinct accounts. Then a "
    "deterministic sample of up to 40 of its accounts is looked up (public AppView profile, PLC "
    "log when the profile has no createdAt): >= 80% must resolve and >= 80% of those must be "
    ">= 365 days old with >= 30 followers and >= 30 posts. Detector output is never used."
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.organic", description=__doc__.split("\n")[0])
    add_arguments(p)
    return main_args(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
