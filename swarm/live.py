"""The live picture index: which pictures did many accounts post on Bluesky, and do they swarm?

One pass (`run_pass`):
1. candidates: image files (posts.media_hashes of posts with images / recordWithMedia) that at
   least `file_min_accounts` distinct accounts posted in the window, not deleted;
2. hashes: perceptual hash of each candidate's CDN thumbnail (cached: a file is fetched once);
3. groups: files that are the same picture are merged (swarm/picture.py); a group needs
   `min_accounts` distinct posting accounts;
4. sightings: every post carrying the picture, plus reposts, likes (capped), quotes and replies of
   those posts; accounts from `accounts`, enriched within a budget (signals/enrich.py);
5. score (swarm/score.py, basis "live"), safety for the public wall (swarm/safety.py), optional
   vision read (kind, caption, adult flag), then upsert into `meme_swarms` (swarm/store.py).

A dry run does all of this against a read-only connection and writes nothing: hashes and
enrichment stay in memory.

`LiveSwarmIndex` is the read side for the lookup: find a stored group by file CID or by picture.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row, tuple_row

from swarm.model import AccountInfo, Sighting, SwarmDetail, SwarmPost, SwarmReport
from swarm.picture import (
    MATCH_DHASH,
    MATCH_PHASH,
    MAYBE_DHASH,
    MAYBE_PHASH,
    PictureHash,
    ThumbFetcher,
    group_files,
    hamming,
    hash_files,
    hash_picture,
    same_picture,
    thumb_url,
)
from swarm.safety import fetch_labels, judge
from swarm.store import (
    MAX_POSTS,
    DbHashCache,
    MemoryHashCache,
    PriorState,
    StoredGroup,
    SwarmStore,
    stable_id,
)

log = logging.getLogger(__name__)

IMAGE_KINDS = ("images", "recordWithMedia")
LAG_GUARD = timedelta(seconds=15)
BRIDGY_PDS = "atproto.brid.gy"


@dataclass
class LiveConfig:
    window_h: float = 24.0
    min_accounts: int = 5  # distinct accounts that posted the picture (any of its files)
    # A file is a candidate from this many accounts: a picture re-uploaded as several files (3 + 3
    # accounts) still reaches min_accounts once its files are merged.
    file_min_accounts: int = 3
    max_files: int = 4000  # candidate files per pass, most accounts first
    hash_budget: int = 2000  # new thumbnails fetched per pass
    max_groups: int | None = None  # score at most this many groups (most accounts first)
    likes_per_post: int = 50
    likes_per_group: int = 400
    reposts_per_group: int = 1000
    replies_per_group: int = 300
    enrich_budget: int = 400  # accounts looked up (getProfiles + PLC) per pass
    label_posts: int = 3  # representative posts per group checked for AppView labels
    vision_budget: int = 20  # vision reads per pass (0 = off; no key = off)
    # with a vision reader configured, a group reaches the wall only once it has been read
    wall_needs_vision: bool = True
    stale_h: float = 6.0  # drop stored groups no pass has seen for this long
    workers: int = 16


class HashCache(Protocol):
    def get(self, cids: list[str]) -> dict[str, PictureHash | None]: ...
    def put(self, found: dict[str, tuple[str, PictureHash | None]]) -> None: ...


Scorer = Callable[..., SwarmReport]
Fetch = Callable[[str], bytes | None]
# (picture bytes, post text for context) -> (kind, caption, adult) or None
VisionReader = Callable[[bytes, str], tuple[str | None, str | None, bool] | None]


def default_scorer() -> Scorer:
    from swarm.score import score_swarm  # lazily: written by another module owner

    return score_swarm


# --- small pure helpers --------------------------------------------------------------------------


def post_url(uri: str) -> str | None:
    """at://did/app.bsky.feed.post/rkey -> https://bsky.app/profile/did/post/rkey"""
    if not uri.startswith("at://"):
        return None
    parts = uri.removeprefix("at://").split("/")
    if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return None


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def account_info(row: dict) -> AccountInfo:
    """AccountInfo from an `accounts` row (or a dry-run overlay with the same keys)."""
    bridged = row.get("pds_host") == BRIDGY_PDS or (row.get("handle") or "").endswith(".brid.gy")
    # Bridged (fediverse) accounts get a new DID when the bridge is set up: not their age.
    born = None if bridged else _utc(row.get("did_created_at") or row.get("created_at"))
    return AccountInfo(
        key=row["did"],
        handle=row.get("handle"),
        name=row.get("display_name"),
        created_at=born,
        followers=row.get("followers"),
        following=row.get("follows"),
        posts_count=row.get("posts_count"),
        bio=row.get("description"),
        has_avatar=row.get("has_avatar"),
        host=row.get("pds_host"),
    )


@dataclass
class Group:
    """One picture: its files and every post carrying any of them."""

    files: list[str]  # earliest first
    posts: list[dict] = field(default_factory=list)  # posts rows, seen_at order
    boosts: dict[str, list[dict]] = field(default_factory=dict)  # kind -> rows
    rep_cid: str = ""
    rep_did: str = ""
    rep_hash: PictureHash | None = None
    id: str = ""
    report: SwarmReport | None = None
    safe: bool = False
    unsafe_reason: str | None = None
    labels_checked: bool = False
    kind: str | None = None
    caption: str | None = None
    vision_done: bool = False
    hold: str | None = None  # not on the wall for now, without a reason that sticks
    prior: PriorState | None = None

    @property
    def dids(self) -> list[str]:
        return list(dict.fromkeys(p["did"] for p in self.posts))

    @property
    def uris(self) -> list[str]:
        return [p["uri"] for p in self.posts]


def merge_same_posts(groups: list[Group]) -> list[Group]:
    """One group per set of posts: the pictures of one multi-image post (4 photos posted together
    by the same accounts) are one card, not four. Files keep their earliest-first order."""
    by_posts: dict[frozenset[str], Group] = {}
    out: list[Group] = []
    for g in groups:
        key = frozenset(g.uris)
        first = by_posts.get(key)
        if first is None:
            by_posts[key] = g
            out.append(g)
        else:
            first.files = [*first.files, *g.files]
    for g in out:
        first_seen = {}
        for p in g.posts:  # seen_at order
            for f in p.get("media_hashes") or []:
                first_seen.setdefault(f, len(first_seen))
        g.files.sort(key=lambda f: first_seen.get(f, len(first_seen)))
    return out


def build_sightings(group: Group) -> list[Sighting]:
    """Scorer input: one post sighting per post, then its reposts, likes, quotes and replies."""
    out: list[Sighting] = []
    seen: set[str] = set()
    file_set = set(group.files)
    for p in group.posts:
        if p["uri"] in seen:
            continue
        seen.add(p["uri"])
        media = [h for h in (p.get("media_hashes") or []) if h in file_set]
        out.append(
            Sighting(
                id=p["uri"],
                platform="bluesky",
                account=p["did"],
                at=_utc(p["seen_at"]),
                kind="post",
                text=p.get("text") or "",
                url=post_url(p["uri"]),
                media_key=media[0] if media else None,
                langs=list(p.get("langs") or []),
            )
        )
    for kind in ("repost", "like", "quote", "reply"):
        for b in group.boosts.get(kind, []):
            if b["uri"] in seen:  # a quote that carries the picture itself is already a post
                continue
            seen.add(b["uri"])
            target = (
                b["quote_uri"]
                if kind == "quote"
                else (b["reply_to"] if kind == "reply" else b["subject_uri"])
            )
            out.append(
                Sighting(
                    id=b["uri"],
                    platform="bluesky",
                    account=b["did"],
                    at=_utc(b["seen_at"]),
                    kind=kind,
                    target=target,
                    text=(b.get("text") or "") if kind in ("quote", "reply") else "",
                    url=post_url(b["uri"]) if kind in ("quote", "reply") else None,
                    langs=list(b.get("langs") or []) if kind in ("quote", "reply") else [],
                )
            )
    return out


def swarm_posts(group: Group, handles: dict[str, str | None]) -> list[SwarmPost]:
    """<= 30 posts as links, earliest first (no label on any author)."""
    out = []
    for p in group.posts[:MAX_POSTS]:
        url = post_url(p["uri"])
        if url:
            out.append(
                SwarmPost(
                    url=url,
                    handle=handles.get(p["did"]),
                    at=_utc(p["seen_at"]),
                    text=(p.get("text") or "")[:500],
                )
            )
    return out


def basis_note(n_posts: int, window_h: float) -> str:
    hours = f"{window_h:.0f}" if window_h >= 1.5 else f"{window_h:.1f}"
    return f"{n_posts} post{'s' if n_posts != 1 else ''} our Bluesky feed saw in {hours} h"


def representative_posts(group: Group, n: int) -> list[str]:
    """Earliest, latest and evenly spread posts in between."""
    uris = group.uris
    if len(uris) <= n:
        return uris
    step = (len(uris) - 1) / max(1, n - 1)
    return list(dict.fromkeys(uris[round(i * step)] for i in range(n)))


def assign_ids(groups: list[Group], prior_by_file: dict[str, list[PriorState]]) -> list[str]:
    """Give each group the id of the oldest stored group it overlaps (by file) that no other group
    of this pass took first, else a short hash of its earliest file. Returns stored ids that were
    merged away (another stored group of the same picture)."""
    used: set[str] = set()
    retired: set[str] = set()
    for g in sorted(groups, key=lambda g: -len(g.dids)):
        prior: list[PriorState] = []
        for f in g.files:
            for st in prior_by_file.get(f, []):
                if st not in prior:
                    prior.append(st)
        prior.sort(key=lambda s: (s.created_at or datetime.max.replace(tzinfo=UTC), s.id))
        chosen = next((s for s in prior if s.id not in used), None)
        if chosen is not None:
            g.id, g.prior = chosen.id, chosen
            retired |= {s.id for s in prior if s.id != chosen.id}
        else:
            g.id = stable_id(g.files[0])
            if g.id in used:  # a split: the earliest file went to another group
                g.id = stable_id("|".join(g.files))
        used.add(g.id)
    return sorted(retired - used)


# --- SQL -----------------------------------------------------------------------------------------


def window_bounds(conn: psycopg.Connection, window_h: float, end: datetime | None = None):
    """(start, end): end is now (minus a lag guard) but never past what ingest has written; start
    is end - window, but not before the oldest posts partition still kept (retention)."""
    if end is None:
        end = datetime.now(UTC) - LAG_GUARD
        with conn.cursor(row_factory=tuple_row) as cur:
            row = cur.execute(
                "SELECT last_event_at FROM ingest_state WHERE stream = 'jetstream'"
            ).fetchone()
        if row and row[0] and row[0] < end:
            end = row[0]
    end = end.replace(microsecond=0)
    start = end - timedelta(hours=window_h)
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            SELECT min(right(c.relname, 10)) FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = 'posts'::regclass AND c.relname ~ '_p[0-9]{10}$'
            """
        ).fetchone()
    if row and row[0]:
        oldest = datetime.strptime(row[0], "%Y%m%d%H").replace(tzinfo=UTC)
        start = max(start, oldest)
    return start, end


def load_candidates(
    conn: psycopg.Connection, start: datetime, end: datetime, min_accounts: int, limit: int
) -> list[dict]:
    """[{cid, did, n_accounts, n_posts, first_at}] of image files posted by >= min_accounts."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT h AS cid, count(DISTINCT p.did) AS n_accounts, count(*) AS n_posts,
                   min(p.seen_at) AS first_at,
                   (array_agg(p.did ORDER BY p.seen_at))[1] AS did
            FROM posts p, unnest(p.media_hashes) AS h
            WHERE p.seen_at >= %(start)s AND p.seen_at < %(end)s
              AND p.embed_kind = ANY(%(kinds)s) AND p.deleted_at IS NULL
              AND p.media_hashes IS NOT NULL
            GROUP BY h
            HAVING count(DISTINCT p.did) >= %(min)s
            ORDER BY count(DISTINCT p.did) DESC, min(p.seen_at)
            LIMIT %(limit)s
            """,
            {
                "start": start,
                "end": end,
                "kinds": list(IMAGE_KINDS),
                "min": min_accounts,
                "limit": limit,
            },
        ).fetchall()


def load_posts(
    conn: psycopg.Connection, cids: list[str], start: datetime, end: datetime
) -> list[dict]:
    if not cids:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT uri, did, text, langs, seen_at, media_hashes, tags, embed_kind
            FROM posts
            WHERE seen_at >= %(start)s AND seen_at < %(end)s
              AND media_hashes && %(cids)s::text[]
              AND embed_kind = ANY(%(kinds)s) AND deleted_at IS NULL
            ORDER BY seen_at, uri
            """,
            {"start": start, "end": end, "cids": cids, "kinds": list(IMAGE_KINDS)},
        ).fetchall()


def load_boosts(
    conn: psycopg.Connection, uris: list[str], start: datetime, end: datetime, likes_per_post: int
) -> dict[str, list[dict]]:
    """Reposts, likes (<= likes_per_post per post, earliest), quotes and replies of these posts."""
    out: dict[str, list[dict]] = {"repost": [], "like": [], "quote": [], "reply": []}
    if not uris:
        return out
    p = {"uris": uris, "start": start, "end": end, "cap": likes_per_post}
    with conn.cursor(row_factory=dict_row) as cur:
        out["repost"] = cur.execute(
            """
            SELECT uri, did, subject_uri, seen_at FROM reposts
            WHERE subject_uri = ANY(%(uris)s) AND seen_at >= %(start)s AND seen_at < %(end)s
              AND deleted_at IS NULL
            ORDER BY seen_at
            """,
            p,
        ).fetchall()
        out["like"] = cur.execute(
            """
            SELECT uri, did, subject_uri, seen_at FROM (
                SELECT uri, did, subject_uri, seen_at,
                       row_number() OVER (PARTITION BY subject_uri ORDER BY seen_at) AS rn
                FROM likes
                WHERE subject_uri = ANY(%(uris)s) AND seen_at >= %(start)s
                  AND seen_at < %(end)s AND deleted_at IS NULL
            ) x WHERE rn <= %(cap)s
            ORDER BY seen_at
            """,
            p,
        ).fetchall()
        out["quote"] = cur.execute(
            """
            SELECT uri, did, text, langs, seen_at, quote_uri FROM posts
            WHERE quote_uri = ANY(%(uris)s) AND seen_at >= %(start)s AND seen_at < %(end)s
              AND deleted_at IS NULL
            ORDER BY seen_at
            """,
            p,
        ).fetchall()
        out["reply"] = cur.execute(
            """
            SELECT uri, did, text, langs, seen_at, reply_to FROM posts
            WHERE reply_to = ANY(%(uris)s) AND seen_at >= %(start)s AND seen_at < %(end)s
              AND deleted_at IS NULL
            ORDER BY seen_at
            """,
            p,
        ).fetchall()
    return out


_ACCOUNT_COLS = """did, handle, display_name, did_created_at, created_at, followers, follows,
    posts_count, description, has_avatar, pds_host,
    (profile_fetched_at IS NOT NULL OR plc_fetched_at IS NOT NULL) AS enriched"""


def load_accounts(conn: psycopg.Connection, dids: list[str]) -> dict[str, dict]:
    if not dids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"SELECT {_ACCOUNT_COLS} FROM accounts WHERE did = ANY(%s)", (dids,)
        ).fetchall()
    return {r["did"]: r for r in rows}


def load_baseline(conn: psycopg.Connection, now: datetime, fresh_days: int = 30, hours: int = 24):
    """The scorer's Baseline from the random active-account sample (signals/baseline.py writes
    `baseline_accounts`), with the scorer's definitions: fresh = created (PLC date, else profile
    date) within `fresh_days`; empty = no avatar and no bio. Bridged accounts have no age."""
    from swarm.score import Baseline

    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            SELECT count(*) FILTER (WHERE born IS NOT NULL),
                   count(*) FILTER (WHERE born > %(now)s - make_interval(days => %(fd)s)),
                   count(*) FILTER (WHERE has_avatar IS NOT NULL),
                   count(*) FILTER (WHERE has_avatar IS NOT NULL AND NOT has_avatar
                                    AND coalesce(description_len, 0) = 0)
            FROM (
              SELECT CASE WHEN a.pds_host = %(bridgy)s OR a.handle LIKE '%%.brid.gy' THEN NULL
                          ELSE coalesce(a.did_created_at, a.created_at) END AS born,
                     a.has_avatar, a.description_len
              FROM baseline_accounts b JOIN accounts a USING (did)
              WHERE b.sampled_at > %(now)s - make_interval(hours => %(h)s)
                AND b.sampled_at <= %(now)s
            ) s
            """,
            {"now": now, "fd": fresh_days, "h": hours, "bridgy": BRIDGY_PDS},
        ).fetchone()
    n_age, n_fresh, n_prof, n_empty = row
    if not n_age and not n_prof:
        return None
    return Baseline(
        fresh_rate=n_fresh / n_age if n_age else None,
        empty_rate=n_empty / n_prof if n_prof else None,
        n=min(n for n in (n_age, n_prof) if n),
    )


# --- enrichment -----------------------------------------------------------------------------------


def priority_dids(groups: list[Group]) -> list[str]:
    """Posting accounts, round-robin over groups (largest first), so a budget covers every group."""
    lists = [g.dids for g in sorted(groups, key=lambda g: -len(g.dids))]
    out: dict[str, None] = {}
    for i in range(max((len(x) for x in lists), default=0)):
        for x in lists:
            if i < len(x):
                out.setdefault(x[i], None)
    return list(out)


def enrich_in_memory(rows: dict[str, dict], dids: list[str], budget: int) -> int:
    """Dry run: getProfiles + PLC for accounts without data, results only in `rows`."""
    import httpx

    from signals.enrich import UA, _ts, fetch_plc, fetch_profiles

    todo = [d for d in dids if not (rows.get(d) or {}).get("enriched")][:budget]
    if not todo:
        return 0
    from concurrent.futures import ThreadPoolExecutor

    with httpx.Client(headers=UA, follow_redirects=True) as client:
        profiles = fetch_profiles(client, todo)
        with ThreadPoolExecutor(max_workers=8) as pool:
            plc = dict(zip(todo, pool.map(lambda d: fetch_plc(client, d), todo), strict=True))
    for d in todo:
        p, q = profiles.get(d), plc.get(d)
        if not p and not q:
            continue
        row = dict(rows.get(d) or {"did": d})
        if p:
            row.update(
                handle=p.get("handle") or row.get("handle"),
                display_name=p.get("displayName"),
                created_at=_ts(p.get("createdAt")) or row.get("created_at"),
                followers=p.get("followersCount"),
                follows=p.get("followsCount"),
                posts_count=p.get("postsCount"),
                description=(p.get("description") or "")[:1000],
                has_avatar=bool(p.get("avatar")),
            )
        if q:
            row.update(did_created_at=q["created"], pds_host=q["pds_host"] or row.get("pds_host"))
        row["enriched"] = True
        rows[d] = row
    return len(todo)


# --- vision (optional) ----------------------------------------------------------------------------


def nemotron_reader() -> VisionReader | None:
    """The wall's vision read (swarm/safety.py WallReader) on the configured Nemotron endpoint,
    or None without a key."""
    from common.config import get_settings
    from swarm.safety import WallReader

    return WallReader.from_settings(get_settings())


# --- the pass ---------------------------------------------------------------------------------


@dataclass
class PassResult:
    groups: list[Group]
    stats: dict[str, Any]
    window_h: float


def run_pass(
    conn: psycopg.Connection,
    cfg: LiveConfig | None = None,
    *,
    dry_run: bool = False,
    scorer: Scorer | None = None,
    fetch: Fetch | None = None,
    hash_cache: HashCache | None = None,
    labels: Callable[[list[str]], dict[str, set[str]]] | None = fetch_labels,
    vision: VisionReader | None = None,
    enrich: bool = True,
    end: datetime | None = None,
) -> PassResult:
    """One pass over the window. With dry_run nothing is written (hashes and enrichment stay in
    memory) and the connection may be read-only."""
    cfg = cfg or LiveConfig()
    scorer = scorer or default_scorer()
    stats: dict[str, Any] = {}
    timings: dict[str, float] = {}
    t_all = time.monotonic()

    def lap(name: str, t0: float) -> float:
        timings[name] = round(time.monotonic() - t0, 2)
        return time.monotonic()

    t = time.monotonic()
    start, end = window_bounds(conn, cfg.window_h, end)
    window_h = (end - start).total_seconds() / 3600
    cands = load_candidates(conn, start, end, cfg.file_min_accounts, cfg.max_files)
    stats.update(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        window_h=round(window_h, 2),
        candidate_files=len(cands),
    )
    t = lap("candidates_s", t)

    # hashes
    cache = (
        hash_cache
        if hash_cache is not None
        else (MemoryHashCache() if dry_run else DbHashCache(conn))
    )
    owned_fetcher = None
    if fetch is None:
        owned_fetcher = fetch = ThumbFetcher(workers=cfg.workers)
    cids = [c["cid"] for c in cands]
    known = cache.get(cids)
    missing = [(c["cid"], c["did"]) for c in cands if c["cid"] not in known][: cfg.hash_budget]
    fetched = hash_files(missing, fetch, workers=cfg.workers) if missing else {}
    did_of = {c["cid"]: c["did"] for c in cands}
    cache.put({cid: (did_of[cid], h) for cid, h in fetched.items()})
    hashes = {**known, **fetched}
    stats.update(
        hashed_new=len(fetched),
        hash_failed=sum(1 for h in fetched.values() if h is None),
        hash_cached=len(known),
    )
    t = lap("hash_s", t)

    # groups (pre-filter on the sum of per-file account counts: an upper bound)
    order = sorted(cands, key=lambda c: (c["first_at"], c["cid"]))
    by_cid = {c["cid"]: c for c in cands}
    raw = group_files([c["cid"] for c in order], hashes)
    raw = [fs for fs in raw if sum(by_cid[f]["n_accounts"] for f in fs) >= cfg.min_accounts]
    posts = load_posts(conn, [f for fs in raw for f in fs], start, end)
    group_of: dict[str, int] = {f: i for i, fs in enumerate(raw) for f in fs}
    groups = [Group(files=fs) for fs in raw]
    for p in posts:
        for gi in dict.fromkeys(group_of[h] for h in (p["media_hashes"] or []) if h in group_of):
            groups[gi].posts.append(p)
    groups = merge_same_posts([g for g in groups if len(g.dids) >= cfg.min_accounts])
    groups.sort(key=lambda g: (-len(g.dids), g.files[0]))
    if cfg.max_groups is not None:
        groups = groups[: cfg.max_groups]
    for g in groups:
        hashed = [f for f in g.files if hashes.get(f) is not None]
        counts = {f: by_cid[f]["n_accounts"] for f in g.files}
        rep = max(hashed or g.files, key=lambda f: (counts[f], -g.files.index(f)))
        g.rep_cid, g.rep_did, g.rep_hash = rep, by_cid[rep]["did"], hashes.get(rep)
    stats.update(
        groups=len(groups),
        merged_groups=sum(1 for g in groups if len(g.files) > 1),
        posts=sum(len(g.posts) for g in groups),
    )
    t = lap("group_s", t)

    # boosts
    all_uris = list(dict.fromkeys(u for g in groups for u in g.uris))
    boosts = load_boosts(conn, all_uris, start, end, cfg.likes_per_post)
    caps = {
        "repost": cfg.reposts_per_group,
        "like": cfg.likes_per_group,
        "quote": cfg.replies_per_group,
        "reply": cfg.replies_per_group,
    }
    target_col = {
        "repost": "subject_uri",
        "like": "subject_uri",
        "quote": "quote_uri",
        "reply": "reply_to",
    }
    by_target: dict[str, dict[str, list[dict]]] = {k: defaultdict(list) for k in boosts}
    for kind, rows in boosts.items():
        for r in rows:
            by_target[kind][r[target_col[kind]]].append(r)
    for g in groups:
        for kind in boosts:
            rows = [r for u in g.uris for r in by_target[kind].get(u, [])]
            rows.sort(key=lambda r: r["seen_at"])
            g.boosts[kind] = rows[: caps[kind]]
    stats["boosts"] = {k: len(v) for k, v in boosts.items()}
    t = lap("boosts_s", t)

    # accounts
    posters = priority_dids(groups)
    boosters = list({b["did"] for g in groups for rows in g.boosts.values() for b in rows})
    rows = load_accounts(conn, list(dict.fromkeys(posters + boosters)))
    n_enriched = 0
    if enrich and cfg.enrich_budget > 0 and posters:
        try:
            if dry_run:
                n_enriched = enrich_in_memory(rows, posters, cfg.enrich_budget)
            else:
                from signals.enrich import enrich as enrich_db

                n_enriched = enrich_db(conn, posters, budget=cfg.enrich_budget)
                rows.update(load_accounts(conn, posters))
        except Exception:
            log.exception("enrichment failed; scoring with what is stored")
    infos = {d: account_info(r) for d, r in rows.items() if r.get("enriched")}
    handles = {d: r.get("handle") for d, r in rows.items()}
    baseline = None
    try:
        baseline = load_baseline(conn, end)
    except Exception:
        log.exception("baseline unavailable")
    stats.update(
        accounts=len(posters),
        accounts_known=sum(1 for d in posters if d in infos),
        enriched=n_enriched,
        baseline_n=getattr(baseline, "n", None),
    )
    t = lap("accounts_s", t)

    # score
    for g in groups:
        sightings = build_sightings(g)
        accounts = {d: infos[d] for d in {s.account for s in sightings} if d in infos}
        g.report = scorer(
            sightings,
            accounts,
            basis="live",
            basis_note=basis_note(sum(1 for s in sightings if s.kind == "post"), window_h),
            baseline=baseline,
        )
    t = lap("score_s", t)

    # ids and what earlier passes already know
    prior_by_file: dict[str, list[PriorState]] = {}
    store = None if dry_run else SwarmStore(conn)
    if store is not None:
        prior_by_file = store.prior(f for g in groups for f in g.files)
    retired = assign_ids(groups, prior_by_file)

    # safety: words and tags first, then AppView labels for a few posts per group
    reps = {g.id: representative_posts(g, cfg.label_posts) for g in groups}
    found: dict[str, set[str]] | None = None
    if labels is not None and groups:
        try:
            found = labels([u for us in reps.values() for u in us])
        except Exception:
            log.exception("label check failed; groups keep their earlier safety")
    for g in groups:
        checked = found is not None
        lab = [found.get(u, set()) for u in reps[g.id]] if found else []
        s = judge(((p.get("text"), p.get("tags")) for p in g.posts), lab)
        if g.prior is not None and g.prior.unsafe_reason:  # once unsafe, stays unsafe
            g.safe, g.unsafe_reason = False, g.prior.unsafe_reason
        elif not s.safe:
            g.safe, g.unsafe_reason = False, s.reason
        elif checked:
            g.safe = True
        else:  # labels not checked this pass: keep what an earlier pass decided
            g.safe = bool(g.prior and g.prior.safe)
        g.labels_checked = checked
        if g.prior is not None:
            g.kind, g.caption = g.prior.kind, g.prior.caption
            g.vision_done = False
    t = lap("safety_s", t)

    # vision: safe groups not read before, in wall order (swarm first, then by score)
    n_vision = 0
    if vision is not None and cfg.vision_budget > 0:
        todo = [
            g
            for g in groups
            if g.safe and g.rep_hash is not None and not (g.prior and g.prior.vision_at)
        ]
        todo.sort(
            key=lambda g: (
                g.report.verdict != "swarm" if g.report else True,
                -(g.report.score if g.report else 0),
                -len(g.dids),
            )
        )
        for g in todo[: cfg.vision_budget]:
            try:
                data = fetch(thumb_url(g.rep_did, g.rep_cid))
                res = vision(data, (g.posts[0].get("text") or "")) if data else None
            except Exception as exc:
                log.warning("vision read failed for %s: %s", g.id, exc)
                if type(exc).__name__ == "NotVisionModelError":
                    break
                continue
            n_vision += 1
            if res is None:
                continue
            g.kind, g.caption, adult = res
            g.vision_done = True
            if adult:
                g.safe, g.unsafe_reason = False, "vision: adult"
    if vision is not None and cfg.wall_needs_vision:
        # Words and labels miss suggestive bait (swimwear selfies are not labelled): with a
        # vision reader configured, only pictures it has read reach the wall.
        for g in groups:
            if g.safe and not g.vision_done and not (g.prior and g.prior.vision_at):
                g.safe, g.hold = False, "vision pending"
    stats["vision_reads"] = n_vision
    stats["unsafe"] = sum(1 for g in groups if g.unsafe_reason)
    stats["pending"] = sum(1 for g in groups if g.hold and not g.unsafe_reason)
    stats["wall"] = sum(1 for g in groups if g.safe)
    t = lap("vision_s", t)

    # store
    if store is not None:
        out = [
            StoredGroup(
                id=g.id,
                files=g.files,
                rep_did=g.rep_did,
                rep_cid=g.rep_cid,
                report=g.report,
                posts=swarm_posts(g, handles),
                phash=g.rep_hash.phash if g.rep_hash else None,
                dhash=g.rep_hash.dhash if g.rep_hash else None,
                safe=g.safe,
                unsafe_reason=g.unsafe_reason,
                labels_checked=g.labels_checked,
                kind=g.kind,
                caption=g.caption,
                vision_done=g.vision_done,
                window_h=round(window_h, 2),
            )
            for g in groups
        ]
        stats["stored"] = store.upsert(out, retired)
        stats["retired"] = len(retired)
        stats["dropped"] = store.drop_stale(cfg.stale_h)
    t = lap("store_s", t)

    if owned_fetcher is not None:
        owned_fetcher.close()
    verdicts: dict[str, int] = defaultdict(int)
    for g in groups:
        verdicts[g.report.verdict if g.report else "none"] += 1
    stats["verdicts"] = dict(verdicts)
    timings["total_s"] = round(time.monotonic() - t_all, 2)
    stats["timings"] = timings
    return PassResult(groups, stats, window_h)


def describe(g: Group) -> str:
    """One dry-run line: verdict, score, families, accounts, posts, safe, first text, thumbnail."""
    r = g.report
    fams = ",".join(f"{k}={v:.2f}" for k, v in (r.families.items() if r else []))
    text = " ".join((g.posts[0].get("text") or "").split())[:60] if g.posts else ""
    safe = "safe" if g.safe else f"UNSAFE({g.unsafe_reason or g.hold or 'labels unchecked'})"
    thumb = (
        thumb_url(g.rep_did, g.rep_cid)
        if g.rep_hash
        else f"(no thumb) {thumb_url(g.rep_did, g.rep_cid)}"
    )
    return (
        f"{g.id} {r.verdict if r else '-':8} {r.score if r else 0:.2f} [{fams}] "
        f"acc={len(g.dids)} posts={len(g.posts)} files={len(g.files)} {safe} "
        f"{text!r} {thumb}"
    )


# --- read side for the lookup ---------------------------------------------------------------------


ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection] | psycopg.Connection]


class LiveSwarmIndex:
    """Find a stored picture group for a lookup.

    `connect()` returns a connection (or a context manager yielding one), opened per call. By
    default only groups that are safe for the public wall are returned. DB errors return None.
    """

    CACHE_S = 60.0

    def __init__(
        self,
        connect: ConnectionFactory | None = None,
        *,
        fetch: Fetch | None = None,
        include_unsafe: bool = False,
    ) -> None:
        if connect is None:
            from common.db import connect as default_connect

            connect = default_connect
        self._connect = connect
        self._fetch = fetch
        self.include_unsafe = include_unsafe
        self._hashes: tuple[float, list[tuple[str, str, str, PictureHash]]] | None = None

    def _store_call(self, fn: Callable[[SwarmStore], Any]) -> Any:
        try:
            with self._connect() as conn:
                return fn(SwarmStore(conn))
        except psycopg.Error as exc:
            log.warning("swarm index unavailable: %s", exc)
            return None

    def find_by_cids(self, cids: list[str]) -> SwarmDetail | None:
        cids = [c for c in cids if c]
        if not cids:
            return None
        return self._store_call(lambda s: s.find_by_cids(cids, include_unsafe=self.include_unsafe))

    def _group_hashes(self) -> list[tuple[str, str, str, PictureHash]]:
        now = time.monotonic()
        if self._hashes is None or now - self._hashes[0] > self.CACHE_S:
            got = self._store_call(lambda s: s.group_hashes(include_unsafe=self.include_unsafe))
            if got is None:
                return []
            self._hashes = (now, got)
        return self._hashes[1]

    def match_picture(self, image: bytes) -> str | None:
        """The id of the stored group showing this picture, or None."""
        try:
            h = hash_picture(image)
        except ValueError:
            return None
        if not h.detailed:
            return None
        strict, maybe = [], []
        for gid, did, cid, gh in self._group_hashes():
            dp, dd = hamming(h.phash, gh.phash), hamming(h.dhash, gh.dhash)
            if same_picture(h, gh, phash=MATCH_PHASH, dhash=MATCH_DHASH):
                strict.append((dp + dd, gid))
            elif gh.detailed and dp <= MAYBE_PHASH and dd <= MAYBE_DHASH:
                maybe.append((dp + dd, gid, did, cid))
        if strict:
            return min(strict)[1]
        for _, gid, did, cid in sorted(maybe)[:3]:
            if self._verify(image, did, cid):
                return gid
        return None

    def _verify(self, image: bytes, did: str, cid: str) -> bool:
        """media.variants.relate on the stored thumbnail: same, resized or cropped."""
        from media.variants import relate

        fetch = self._fetch or ThumbFetcher(workers=1)
        try:
            data = fetch(thumb_url(did, cid))
            if not data:
                return False
            return relate(data, image).kind in ("same", "resized", "cropped")
        except ValueError:
            return False
        finally:
            if self._fetch is None:
                fetch.close()  # type: ignore[union-attr]

    def find_by_picture(self, image: bytes) -> SwarmDetail | None:
        gid = self.match_picture(image)
        if gid is None:
            return None
        return self._store_call(lambda s: s.detail(gid, include_unsafe=self.include_unsafe))
