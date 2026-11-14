"""The live picture index: which pictures did many accounts post on Bluesky, and do they swarm?

One pass (`run_pass`), bounded by a deadline (LiveConfig.deadline_s):
1. candidates: image files (posts.media_hashes of posts with images / recordWithMedia) that at
   least `file_min_accounts` distinct accounts posted in the window, not deleted;
2. hashes: perceptual hash of each candidate's CDN thumbnail (cached: a file is fetched once);
3. groups: files that are the same picture are merged (swarm/picture.py); a group needs
   `min_accounts` distinct posting accounts;
4. sightings: every post carrying the picture, plus reposts, likes, quotes and replies of those
   posts (capped per post in SQL and per group); accounts from `accounts`, enriched within a
   budget (signals/enrich.py);
5. score (swarm/score.py, basis "live"), safety for the public wall (swarm/safety.py), vision read
   (kind, caption, adult flag), then upsert into `meme_swarms` (swarm/store.py).

Safety is one-sided and fails closed:
- unsafe sticks per file (picture_hashes.unsafe_reason): a group is unsafe when any of its files,
  or any stored group it overlaps, ever was, whatever ids, merges or splits happened since;
- a group is held off the wall (not marked unsafe) while any representative post could not be
  checked for AppView labels or is missing there, while many of its accounts are taken down, and
  while the vision model has not read its *current* picture (vision_cid = rep_cid). Without a
  vision reader nothing new reaches the wall unless LiveConfig.wall_without_vision is set.

A dry run does all of this against a read-only connection and writes nothing: hashes and
enrichment stay in memory (stored groups and unsafe marks are read, never written).

`LiveSwarmIndex` is the read side for the lookup: find a stored group by file CID or by picture,
without any network call.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import numpy as np
import psycopg
from psycopg.rows import dict_row, tuple_row

from swarm.model import AccountInfo, Sighting, SwarmDetail, SwarmPost, SwarmReport
from swarm.picture import (
    MATCH_DHASH,
    MATCH_PHASH,
    MAYBE_DHASH,
    MAYBE_PHASH,
    VERIFY_BLOCK_LIMIT,
    VERIFY_MAX_LIMIT,
    PictureHash,
    ThumbFetcher,
    aspect_close,
    group_files,
    hash_files,
    hash_picture,
    pixels_close,
    same_picture,
    thumb_url,
)
from swarm.safety import fetch_labels, judge, text_marker
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
# accounts.status values whose posts can no longer be opened (links would be dead)
GONE_STATUSES = frozenset({"takendown", "suspended", "deleted", "deactivated"})
TAKEN_DOWN = frozenset({"takendown", "suspended"})
LABEL_BATCH = 100  # representative posts per label call (4 getPosts requests)
ARRAY_CHUNK = 5000


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
    reposts_per_post: int = 100
    replies_per_post: int = 50  # quotes and replies, each
    likes_per_group: int = 400
    reposts_per_group: int = 1000
    replies_per_group: int = 300
    boost_posts_per_group: int = 1000  # earliest posts of a group whose boosts are loaded
    boost_chunk: int = 1000  # post uris per boost query
    enrich_budget: int = 400  # accounts looked up (getProfiles + PLC) per pass
    label_posts: int = 5  # representative posts per group checked for AppView labels
    vision_budget: int = 20  # vision reads per pass (0 = off)
    # Without a vision reader (no key, reader broken) nothing new reaches the wall, unless this
    # is set (python -m swarm --wall-without-vision / SWARM_WALL_WITHOUT_VISION=1).
    wall_without_vision: bool = False
    # this share of a group's posting accounts taken down or suspended: held off the wall
    takedown_hold_share: float = 0.25
    stale_h: float = 6.0  # drop stored groups no pass has seen for this long
    hash_keep_h: float = 48.0  # prune unused hash rows after this (unsafe marks are kept)
    deadline_s: float = 240.0  # no new hash, label or vision batch starts after this
    workers: int = 16


class HashCache(Protocol):
    def get(self, cids: list[str]) -> dict[str, PictureHash | None]: ...
    def put(self, found: dict[str, tuple[str, PictureHash | None]]) -> None: ...


class Scorer(Protocol):
    def __call__(
        self,
        sightings: list[Sighting],
        accounts: dict[str, AccountInfo] | None = None,
        *,
        basis: str = ...,
        basis_note: str = ...,
        baseline: Any = ...,
        window: tuple[datetime, datetime] | None = ...,
        selected_by_file: bool = ...,
    ) -> SwarmReport: ...


Fetch = Callable[[str], bytes | None]
# picture bytes -> (kind, caption, adult) or None. Only the picture: never post text.
VisionReader = Callable[[bytes], tuple[str | None, str | None, bool] | None]
Labels = Callable[[list[str]], dict[str, set[str]]]


def default_scorer() -> Scorer:
    from swarm.score import score_swarm  # lazily: written by another module owner

    return score_swarm


def scorer_extras(scorer: Callable, start: datetime, end: datetime) -> dict[str, Any]:
    """window= and selected_by_file= for a scorer that takes them (older scorers don't)."""
    try:
        params = inspect.signature(scorer).parameters
    except (TypeError, ValueError):
        return {}
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    out: dict[str, Any] = {}
    if var_kw or "window" in params:
        out["window"] = (start, end)
    if var_kw or "selected_by_file" in params:
        out["selected_by_file"] = True
    return out


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
    vision_at: datetime | None = None
    vision_cid: str | None = None  # the file the vision read saw
    hold: str | None = None  # not on the wall for now, without a reason that sticks
    prior: PriorState | None = None  # the stored group whose id this group took
    priors: list[PriorState] = field(default_factory=list)  # every stored group it overlaps

    @property
    def dids(self) -> list[str]:
        return list(dict.fromkeys(p["did"] for p in self.posts))

    @property
    def uris(self) -> list[str]:
        return [p["uri"] for p in self.posts]

    @property
    def vision_done(self) -> bool:
        """The vision model has read the picture this group shows now."""
        return self.vision_at is not None and bool(self.rep_cid) and self.vision_cid == self.rep_cid


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


def swarm_posts(
    group: Group, handles: dict[str, str | None], gone: set[str] | frozenset[str] = frozenset()
) -> list[SwarmPost]:
    """<= 30 posts as links, earliest first (no label on any author); posts of accounts that are
    gone (taken down, deleted) are left out."""
    out = []
    for p in group.posts:
        if len(out) >= MAX_POSTS:
            break
        url = post_url(p["uri"])
        if url and p["did"] not in gone:
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
    of this pass took first, else a short hash of its earliest file (salted until unused). Every
    overlapped stored group is kept in `g.priors`. Returns stored ids that were merged away."""
    used: set[str] = set()
    retired: set[str] = set()
    for g in sorted(groups, key=lambda g: (-len(g.dids), g.files[0] if g.files else "")):
        prior: list[PriorState] = []
        for f in g.files:
            for st in prior_by_file.get(f, []):
                if st not in prior:
                    prior.append(st)
        prior.sort(key=lambda s: (s.created_at or datetime.max.replace(tzinfo=UTC), s.id))
        g.priors = prior
        chosen = next((s for s in prior if s.id not in used), None)
        if chosen is not None:
            g.id, g.prior = chosen.id, chosen
            retired |= {s.id for s in prior if s.id != chosen.id}
        else:
            g.id = stable_id(g.files[0])
            n = 1
            while g.id in used:  # a split: the earliest file's id went to another group
                g.id = stable_id(f"{g.files[0]}#{n}")
                n += 1
        used.add(g.id)
    return sorted(retired - used)


def vision_order(groups: list[Group]) -> list[Group]:
    """Round-robin over verdicts (swarm, organic, unclear), the largest group first within each:
    a pass's reads reach organic memes too, not only the swarm groups whose scores tie at 1.0."""
    buckets: dict[str, list[Group]] = defaultdict(list)
    for g in groups:
        buckets[g.report.verdict if g.report else "unclear"].append(g)
    lists = []
    for v in (
        "swarm",
        "organic",
        "unclear",
        *sorted(set(buckets) - {"swarm", "organic", "unclear"}),
    ):
        if buckets.get(v):
            lists.append(
                sorted(
                    buckets[v],
                    key=lambda g: (-len(g.dids), -(g.report.score if g.report else 0), g.id),
                )
            )
    out = []
    for i in range(max((len(x) for x in lists), default=0)):
        out += [x[i] for x in lists if i < len(x)]
    return out


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
    """[{cid, did, n_accounts, n_posts, first_at}] of image files posted by >= min_accounts.
    A full scan of the window's image posts (2.4-3.0 s on production, 14 Nov): bounded by the
    connection's statement_timeout (swarm/__main__.py)."""
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
    conn: psycopg.Connection,
    uris: list[str],
    start: datetime,
    end: datetime,
    likes_per_post: int,
    reposts_per_post: int = 100,
    replies_per_post: int = 50,
) -> dict[str, list[dict]]:
    """Reposts, likes, quotes and replies of these posts, each capped per post in SQL (the
    earliest), so one viral post cannot flood the worker."""
    out: dict[str, list[dict]] = {"repost": [], "like": [], "quote": [], "reply": []}
    if not uris:
        return out
    p = {
        "uris": uris,
        "start": start,
        "end": end,
        "likes": likes_per_post,
        "reposts": reposts_per_post,
        "replies": replies_per_post,
    }
    queries = {
        "repost": ("uri, did, subject_uri, seen_at", "reposts", "subject_uri", "reposts"),
        "like": ("uri, did, subject_uri, seen_at", "likes", "subject_uri", "likes"),
        "quote": ("uri, did, text, langs, seen_at, quote_uri", "posts", "quote_uri", "replies"),
        "reply": ("uri, did, text, langs, seen_at, reply_to", "posts", "reply_to", "replies"),
    }
    with conn.cursor(row_factory=dict_row) as cur:
        for kind, (cols, table, target, cap) in queries.items():
            out[kind] = cur.execute(
                f"""
                SELECT {cols} FROM (
                    SELECT {cols},
                           row_number() OVER (PARTITION BY {target} ORDER BY seen_at) AS rn
                    FROM {table}
                    WHERE {target} = ANY(%(uris)s) AND seen_at >= %(start)s
                      AND seen_at < %(end)s AND deleted_at IS NULL
                ) x WHERE rn <= %({cap})s
                ORDER BY seen_at
                """,
                p,
            ).fetchall()
    return out


_ACCOUNT_COLS = """did, handle, display_name, did_created_at, created_at, followers, follows,
    posts_count, description, has_avatar, pds_host, status,
    (profile_fetched_at IS NOT NULL OR plc_fetched_at IS NOT NULL) AS enriched"""


def load_accounts(conn: psycopg.Connection, dids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    dids = list(dict.fromkeys(dids))
    for i in range(0, len(dids), ARRAY_CHUNK):
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                f"SELECT {_ACCOUNT_COLS} FROM accounts WHERE did = ANY(%s)",
                (dids[i : i + ARRAY_CHUNK],),
            ).fetchall()
        out.update({r["did"]: r for r in rows})
    return out


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


# --- vision ---------------------------------------------------------------------------------------


def nemotron_reader() -> VisionReader | None:
    """The wall's vision read (swarm/safety.py WallReader) on the configured Nemotron endpoint,
    or None without a key."""
    from common.config import get_settings
    from swarm.safety import WallReader

    return WallReader.from_settings(get_settings())


def _stops_vision(exc: BaseException) -> bool:
    """A failure that the next read would hit too: wrong model, timeout, endpoint unreachable."""
    names = {t.__name__ for t in type(exc).__mro__}
    return "NotVisionModelError" in names or any(("Timeout" in n or "Connect" in n) for n in names)


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
    labels: Labels | None = fetch_labels,
    vision: VisionReader | None = None,
    enrich: bool = True,
    end: datetime | None = None,
) -> PassResult:
    """One pass over the window. With dry_run nothing is written (hashes and enrichment stay in
    memory) and the connection may be read-only."""
    cfg = cfg or LiveConfig()
    owned_fetcher = None
    if fetch is None:
        owned_fetcher = fetch = ThumbFetcher(workers=cfg.workers)
    try:
        return _run_pass(conn, cfg, dry_run, scorer, fetch, hash_cache, labels, vision, enrich, end)
    finally:
        if owned_fetcher is not None:
            owned_fetcher.close()


def _run_pass(
    conn: psycopg.Connection,
    cfg: LiveConfig,
    dry_run: bool,
    scorer: Scorer | None,
    fetch: Fetch,
    hash_cache: HashCache | None,
    labels: Labels | None,
    vision: VisionReader | None,
    enrich: bool,
    end: datetime | None,
) -> PassResult:
    scorer = scorer or default_scorer()
    stats: dict[str, Any] = {}
    timings: dict[str, float] = {}
    t_all = time.monotonic()
    deadline = t_all + cfg.deadline_s
    late: list[str] = []  # steps cut short by the deadline

    def in_time(step: str) -> bool:
        if time.monotonic() < deadline:
            return True
        if step not in late:
            late.append(step)
            log.warning("swarm pass deadline (%.0f s) reached before %s", cfg.deadline_s, step)
        return False

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

    # hashes, in batches so the deadline is checked between them
    cache = (
        hash_cache
        if hash_cache is not None
        else (MemoryHashCache() if dry_run else DbHashCache(conn))
    )
    cids = [c["cid"] for c in cands]
    known = cache.get(cids)
    missing = [(c["cid"], c["did"]) for c in cands if c["cid"] not in known][: cfg.hash_budget]
    did_of = {c["cid"]: c["did"] for c in cands}
    fetched: dict[str, PictureHash | None] = {}
    step = max(1, cfg.workers * 4)
    for i in range(0, len(missing), step):
        if not in_time("hashes"):
            break
        got = hash_files(missing[i : i + step], fetch, workers=cfg.workers)
        cache.put({cid: (did_of[cid], h) for cid, h in got.items()})
        fetched.update(got)
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
    del posts
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

    # boosts: the earliest posts of each group, in chunks, trimmed to the group caps as they come
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
    groups_of_uri: dict[str, list[int]] = defaultdict(list)
    for gi, g in enumerate(groups):
        g.boosts = {k: [] for k in caps}
        for u in dict.fromkeys(g.uris[: cfg.boost_posts_per_group]):
            groups_of_uri[u].append(gi)
    all_uris = list(groups_of_uri)
    loaded = dict.fromkeys(caps, 0)
    for i in range(0, len(all_uris), max(1, cfg.boost_chunk)):
        chunk = load_boosts(
            conn,
            all_uris[i : i + cfg.boost_chunk],
            start,
            end,
            cfg.likes_per_post,
            cfg.reposts_per_post,
            cfg.replies_per_post,
        )
        touched: set[tuple[int, str]] = set()
        for kind, rows in chunk.items():
            loaded[kind] += len(rows)
            for r in rows:
                for gi in groups_of_uri.get(r[target_col[kind]], ()):
                    groups[gi].boosts[kind].append(r)
                    touched.add((gi, kind))
        for gi, kind in touched:
            rows = groups[gi].boosts[kind]
            if len(rows) > caps[kind]:
                rows.sort(key=lambda r: r["seen_at"])
                del rows[caps[kind] :]
    for g in groups:
        for rows in g.boosts.values():
            rows.sort(key=lambda r: r["seen_at"])
    stats["boosts"] = loaded
    t = lap("boosts_s", t)

    # accounts
    posters = priority_dids(groups)
    boosters = list({b["did"] for g in groups for rows in g.boosts.values() for b in rows})
    rows = load_accounts(conn, posters + boosters)
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
    status = {d: r.get("status") for d, r in rows.items() if r.get("status")}
    gone = {d for d, s in status.items() if s in GONE_STATUSES}
    baseline = None
    try:
        baseline = load_baseline(conn, end)
    except Exception:
        log.exception("baseline unavailable")
    stats.update(
        accounts=len(posters),
        accounts_known=sum(1 for d in posters if d in infos),
        accounts_gone=sum(1 for d in posters if d in gone),
        enriched=n_enriched,
        baseline_n=getattr(baseline, "n", None),
    )
    t = lap("accounts_s", t)

    # score
    extras = scorer_extras(scorer, start, end)
    for g in groups:
        sightings = build_sightings(g)
        accounts = {d: infos[d] for d in {s.account for s in sightings} if d in infos}
        g.report = scorer(
            sightings,
            accounts,
            basis="live",
            basis_note=basis_note(sum(1 for s in sightings if s.kind == "post"), window_h),
            baseline=baseline,
            **extras,
        )
    t = lap("score_s", t)

    # ids, and what earlier passes know: stored groups and per-file unsafe marks (read in a dry
    # run too: they are reads)
    store = SwarmStore(conn)
    all_files = [f for g in groups for f in g.files]
    prior_by_file = store.prior(all_files)
    unsafe_files = store.unsafe_files(all_files)
    retired = assign_ids(groups, prior_by_file)

    # safety: sticky marks, words and tags, AppView labels of a few posts per group
    reps = [representative_posts(g, cfg.label_posts) for g in groups]
    found: dict[str, set[str]] = {}
    failed: set[str] = set()
    if labels is not None:
        want = list(dict.fromkeys(u for us in reps for u in us))
        for i in range(0, len(want), LABEL_BATCH):
            batch = want[i : i + LABEL_BATCH]
            if not in_time("labels"):
                failed.update(want[i:])
                break
            try:
                got = labels(batch)
            except Exception:
                log.exception("label check failed; those groups are held")
                failed.update(batch)
                continue
            found.update(got)
            failed.update(getattr(got, "failed", ()))
    n_unchecked = 0
    for g, rep_uris in zip(groups, reps, strict=True):
        sticky = next(
            (
                r
                for r in (
                    *(p.unsafe_reason for p in g.priors),
                    *(unsafe_files.get(f) for f in g.files),
                )
                if r
            ),
            None,
        )
        checked = labels is not None and all(u in found for u in rep_uris)
        s = judge(
            ((p.get("text"), p.get("tags")) for p in g.posts),
            [found[u] for u in rep_uris if u in found],
        )
        g.labels_checked = checked
        if sticky:  # once unsafe (any file, any overlapping stored group), stays unsafe
            g.safe, g.unsafe_reason = False, sticky
        elif not s.safe:
            g.safe, g.unsafe_reason = False, s.reason
        elif not checked:
            n_unchecked += 1
            g.safe = False
            unchecked = labels is None or any(u in failed for u in rep_uris)
            g.hold = "labels unchecked" if unchecked else "posts missing on AppView"
        else:
            g.safe = True
        dids = g.dids
        down = sum(1 for d in dids if status.get(d) in TAKEN_DOWN)
        if g.safe and dids and down / len(dids) >= cfg.takedown_hold_share:
            g.safe, g.hold = False, "accounts taken down"
        # an earlier vision read counts only while it is a read of the picture shown now (from
        # whichever stored group made it: a split keeps the read of the file it still shows)
        read = next(
            (p for p in g.priors if p.vision_at and p.vision_cid and p.vision_cid == g.rep_cid),
            None,
        )
        if read is not None:
            g.kind, g.caption = read.kind, read.caption
            g.vision_at, g.vision_cid = read.vision_at, read.vision_cid
    stats["labels_unchecked"] = n_unchecked
    t = lap("safety_s", t)

    # vision: safe groups whose current picture was not read, round-robin over verdicts
    n_vision = 0
    if vision is not None and cfg.vision_budget > 0:
        todo = vision_order([g for g in groups if g.safe and g.rep_hash and not g.vision_done])
        for g in todo[: cfg.vision_budget]:
            if not in_time("vision"):
                break
            try:
                data = fetch(thumb_url(g.rep_did, g.rep_cid))
                res = vision(data) if data else None
            except Exception as exc:
                log.warning("vision read failed for %s: %s", g.id, exc)
                if _stops_vision(exc):
                    log.warning("vision off for the rest of this pass")
                    break
                continue
            n_vision += 1
            if res is None:
                continue
            kind, caption, adult = res
            g.kind, g.caption = kind, caption
            g.vision_at, g.vision_cid = datetime.now(UTC), g.rep_cid
            marker = text_marker(caption) if caption else None
            if adult:
                g.safe, g.unsafe_reason, g.caption = False, "vision: adult", None
            elif marker:
                g.safe, g.unsafe_reason, g.caption = False, f"caption {marker}", None
    elif not cfg.wall_without_vision:
        log.warning("no vision reader: pictures not read before are held off the wall")
    if not cfg.wall_without_vision:
        # Words and labels miss suggestive bait (swimwear selfies are not labelled): only
        # pictures the vision model has read reach the wall.
        for g in groups:
            if g.safe and not g.vision_done:
                g.safe, g.hold = False, "vision pending"
    stats["vision_reads"] = n_vision
    stats["unsafe"] = sum(1 for g in groups if g.unsafe_reason)
    stats["pending"] = sum(1 for g in groups if g.hold and not g.unsafe_reason)
    stats["wall"] = sum(1 for g in groups if g.safe)
    t = lap("vision_s", t)

    # store
    if not dry_run:
        out = [
            StoredGroup(
                id=g.id,
                files=g.files,
                rep_did=g.rep_did,
                rep_cid=g.rep_cid,
                report=g.report,
                posts=swarm_posts(g, handles, gone),
                phash=g.rep_hash.phash if g.rep_hash else None,
                dhash=g.rep_hash.dhash if g.rep_hash else None,
                safe=g.safe,
                unsafe_reason=g.unsafe_reason,
                labels_checked=g.labels_checked,
                kind=g.kind,
                caption=g.caption,
                vision_at=g.vision_at,
                vision_cid=g.vision_cid,
                window_h=round(window_h, 2),
            )
            for g in groups
        ]
        marks = {
            f: (by_cid[f]["did"], g.unsafe_reason)
            for g in groups
            if g.unsafe_reason
            for f in g.files
            if f not in unsafe_files
        }
        stats["stored"] = store.upsert(out, retired, marks)
        stats["marked_unsafe"] = len(marks)
        stats["retired"] = len(retired)
        stats["dropped"] = store.drop_stale(cfg.stale_h)
        try:
            stats["hashes_pruned"] = store.prune_hashes(cfg.hash_keep_h)
        except psycopg.Error:
            log.exception("hash prune failed")
    t = lap("store_s", t)

    verdicts: dict[str, int] = defaultdict(int)
    for g in groups:
        verdicts[g.report.verdict if g.report else "none"] += 1
    stats["verdicts"] = dict(verdicts)
    stats["late"] = late
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

    `connect()` returns a connection (or a context manager yielding one), opened per call. Only
    groups that are safe for the public wall and fresh (store.SwarmStore) are returned, unless
    include_unsafe. DB errors return None. No network call: a picture is matched against the
    stored hashes and 64x64 pixels of the groups' files (cached CACHE_S).
    """

    CACHE_S = 60.0

    def __init__(
        self,
        connect: ConnectionFactory | None = None,
        *,
        include_unsafe: bool = False,
        fresh_min: float | None = None,
    ) -> None:
        if connect is None:
            from common.db import connect as default_connect

            connect = default_connect
        self._connect = connect
        self.include_unsafe = include_unsafe
        self.fresh_min = fresh_min
        self._hashes: tuple[float, _HashTable] | None = None

    def _store_call(self, fn: Callable[[SwarmStore], Any]) -> Any:
        try:
            with self._connect() as conn:
                return fn(SwarmStore(conn, fresh_min=self.fresh_min))
        except psycopg.Error as exc:
            log.warning("swarm index unavailable: %s", exc)
            return None

    def find_by_cids(self, cids: list[str]) -> SwarmDetail | None:
        cids = [c for c in cids if c]
        if not cids:
            return None
        return self._store_call(lambda s: s.find_by_cids(cids, include_unsafe=self.include_unsafe))

    def _table(self) -> _HashTable | None:
        now = time.monotonic()
        if self._hashes is None or now - self._hashes[0] > self.CACHE_S:
            got = self._store_call(lambda s: s.group_hashes(include_unsafe=self.include_unsafe))
            if got is None:
                return None
            self._hashes = (now, _HashTable(got))
        return self._hashes[1]

    def match_picture(self, image: bytes) -> str | None:
        """The id of the stored group showing this picture, or None."""
        try:
            h = hash_picture(image)
        except ValueError:
            return None
        if not h.detailed:
            return None
        table = self._table()
        if table is None or not table.rows:
            return None
        return table.match(h)

    def find_by_picture(self, image: bytes) -> SwarmDetail | None:
        gid = self.match_picture(image)
        if gid is None:
            return None
        return self._store_call(lambda s: s.detail(gid, include_unsafe=self.include_unsafe))


class _HashTable:
    """Stored file hashes as arrays: one lookup is a vectorised Hamming distance plus a pixel
    check of at most a few close candidates."""

    def __init__(self, rows: list[tuple[str, str, str, PictureHash]]) -> None:
        self.rows = rows
        self.p = np.array([int(r[3].phash, 16) for r in rows], dtype=np.uint64)
        self.d = np.array([int(r[3].dhash, 16) for r in rows], dtype=np.uint64)

    def match(self, h: PictureHash) -> str | None:
        dp = np.bitwise_count(self.p ^ np.uint64(int(h.phash, 16)))
        dd = np.bitwise_count(self.d ^ np.uint64(int(h.dhash, 16)))
        near = np.nonzero((dp <= MAYBE_PHASH) & (dd <= MAYBE_DHASH))[0]
        strict, maybe = [], []
        for i in sorted(near, key=lambda i: int(dp[i]) + int(dd[i])):
            gid, _did, _cid, gh = self.rows[i]
            dist = int(dp[i]) + int(dd[i])
            if same_picture(h, gh, phash=MATCH_PHASH, dhash=MATCH_DHASH):
                strict.append((dist, gid))
            elif (
                len(maybe) < 3
                and gh.detailed
                and aspect_close(h, gh)
                and pixels_close(h, gh, VERIFY_BLOCK_LIMIT, VERIFY_MAX_LIMIT)
            ):
                maybe.append((dist, gid))
        if strict:
            return min(strict)[1]
        return min(maybe)[1] if maybe else None
