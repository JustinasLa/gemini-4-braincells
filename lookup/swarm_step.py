"""The lookup's swarm check: do the posts that carry this picture act like a swarm?

It turns what the lookup kept into the scorer's input (swarm/model.py: `Sighting`,
`AccountInfo`) and scores it (`Deps.swarm_scorer`, swarm.score.score_swarm). The posts are a
search sample, so the report says `basis: "sample"`. When the live Bluesky index
(`Deps.live_swarms`, swarm.live.LiveSwarmIndex) holds the same picture, its group (every post
our firehose saw in 24 h, already scored) is kept too, and shown when it has more posting
accounts than the sample.

Pure CPU apart from one optional batched Bluesky getProfiles call and the live index query,
all inside a hard cap (SWARM_CAP_S, never into the primary reading's reserve). The sample's
report is published as soon as it is scored; the live query has its own shorter limit and can
only add to it. The lookup keeps a small reference to the live group (`SwarmLiveRef`), not the
group. It never fails the lookup and never changes `status`, `origin_id` or anything else: the
report is additional evidence about the group of posts, never a label on one account.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from lookup.models import Account, Edge, LookupResult, Post
from lookup.threadvariants import _media_key
from swarm.model import AccountInfo, Sighting, SwarmDetail, SwarmLiveRef, SwarmReport

if TYPE_CHECKING:
    from lookup.service import Lookup

log = logging.getLogger(__name__)

SWARM_CAP_S = 2.0  # the whole step, getProfiles and the live index included
LIVE_CAP_S = 1.0  # the live index query, beside the sample (it only adds to the result)
MIN_LEFT_S = 0.3  # less time than this before the reading's reserve: the step is skipped
PROFILES_PER_CALL = 25  # app.bsky.actor.getProfiles' own limit
PROFILES_CALLS = 2
PROFILES_CAP_S = 1.2  # one getProfiles call; the scoring needs the rest of the cap
UPLOAD = "upload:"  # lookup.service.UPLOAD: the uploaded image's placeholder post

# roles of posts that carry the meme (lookup/models.py Post.role) and the action each one is.
# Not counted: mention (a search hit or a post the subject quotes or replies to, not shown to
# carry it), author_recent (the author's other posts), screenshotted (the post a screenshot
# shows), and the upload placeholder (no account, no time).
KIND = {
    "submitted": "post",
    "origin": "post",
    "copy": "post",
    "bluesky": "post",
    "quote": "quote",
    "reply": "reply",
}
_X_MEDIA_SKIP = ("/card_img/", "/profile_images/", "/profile_banners/")
# https://cdn.bsky.app/img/feed_fullsize/plain/<did>/<blob cid>@jpeg
_BSKY_CDN = re.compile(
    r"^https://cdn\.bsky\.app/img/[^/]+/plain/did:[^/]+/([a-z0-9]{20,})(?:@\w+)?$"
)


class LiveSwarms(Protocol):
    """swarm.live.LiveSwarmIndex: the stored live group of a picture, or None."""

    def find_by_cids(self, cids: list[str]) -> SwarmDetail | None: ...  # Bluesky blob CIDs

    def find_by_picture(self, image: bytes) -> SwarmDetail | None: ...  # perceptual hash


# ------------------------------------------------------------------ input building (pure)
def media_key(url: str) -> str | None:
    """The exact file a media URL serves: an X media key (pbs.twimg.com/media/<key>.jpg, as
    lookup/threadvariants.py keys it) or a Bluesky blob CID (cdn.bsky.app). None otherwise
    (link-card thumbnails, avatars, the upload, other hosts)."""
    if m := _BSKY_CDN.match(url or ""):
        return m.group(1)
    try:
        u = httpx.URL(url)
    except Exception:
        return None
    if u.host != "pbs.twimg.com" or u.path.startswith(_X_MEDIA_SKIP):
        return None
    return _media_key(url) or None


def bsky_cid(url: str) -> str | None:
    m = _BSKY_CDN.match(url or "")
    return m.group(1) if m else None


def _did(uri: str) -> str | None:
    """at://did:plc:abc/app.bsky.feed.post/xyz -> did:plc:abc"""
    if uri.startswith("at://did:"):
        return uri.removeprefix("at://").split("/", 1)[0]
    return None


def account_key(p: Post) -> str | None:
    """Sighting.account: "x:<handle lowercased>" on X, the DID (from the post's at:// uri, else
    the handle) on Bluesky."""
    if p.platform == "x":
        return f"x:{p.author_handle.lower()}" if p.author_handle else None
    if p.platform == "bluesky":
        return _did(p.id) or (p.author_handle.lower() if p.author_handle else None)
    return None


def has_picture(p: Post | None) -> bool:
    return bool(p and any(media_key(u) or u.startswith("/api/lookup/") for u in p.media_urls))


def counted_posts(r: LookupResult) -> list[Post]:
    """The posts that carry the meme: the submitted post and the copies, quotes, replies,
    Bluesky posts and origin the lookup kept. For an upload, a post counts only with a caption
    run (`caption_match`, what `_keep_caption_matches` keeps it for)."""
    image = r.kind == "image"
    out = []
    for p in r.posts:
        if p.platform == "web" or p.id.startswith(UPLOAD):
            continue
        # an upload's "mention" read on its own page that repeats the caption carries the meme
        # too: its wording was only too far from the caption to be called a copy
        carrier = image and p.role == "mention" and p.verified and bool(p.caption_match)
        if p.role not in KIND and not carrier:
            continue
        if image and not p.caption_match:
            continue
        if account_key(p) is None:
            continue
        out.append(p)
    return out


def _own_text(p: Post) -> str:
    """The words the account posted: an X post that quotes another carries the quoted text
    after "\\n\\n> " (lookup/service.py); that part is someone else's."""
    if p.platform == "x" and p.role in ("submitted", "quote"):
        return p.text.split("\n\n> ", 1)[0].strip()
    return p.text


def _target(p: Post, rel: str, edges: list[Edge], ids: set[str], subject: str | None) -> str | None:
    """The sighting a quote or reply boosts: the post its edge comes from, else the subject."""
    for e in edges:
        if e.rel == rel and e.to == p.id and e.from_ in ids and e.from_ != p.id:
            return e.from_
    return subject if subject in ids and subject != p.id else None


def quoting_posts(r: LookupResult, quoting: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """X posts that quote another post: `quoting` (the lookup's record of every X post read with
    a quoted post) and every post a "quote" edge points to (Edge from the quoted post)."""
    return set(quoting) | {e.to for e in r.edges if e.rel == "quote"}


def _own_keys(p: Post, quotes: set[str]) -> list[str]:
    """The files this account uploaded with the post. An X post that quotes another lists the
    quoted post's pictures with its own (FxTwitter and the page parser both do): those are the
    quoted account's upload, and which ones they are isn't known, so none count as its own."""
    if p.platform == "x" and p.id in quotes:
        return []
    return [k for u in p.media_urls if (k := media_key(u))]


def build_sightings(
    r: LookupResult, quoting: set[str] | frozenset[str] = frozenset()
) -> list[Sighting]:
    """The counted posts as Sightings. For an X or Bluesky post lookup a "post" sighting needs a
    picture of its own (a text match without one did not post the picture); the submitted post
    always counts. An upload's posts count by their caption run (counted_posts)."""
    posts = counted_posts(r)
    quotes = quoting_posts(r, quoting)
    image = r.kind == "image"
    subject = r.subject_id if r.subject_id in {p.id for p in posts} else None
    subj = next((p for p in posts if p.id == subject), None)
    subject_keys = {k for u in (subj.media_urls if subj else []) if (k := media_key(u))}
    kept = []
    for p in posts:
        kind = KIND.get(p.role, "post")  # a counted mention is a post of the picture
        # a quote shows the quoted post's picture: that file is the target's
        keys = [] if kind == "quote" else _own_keys(p, quotes)
        if kind == "post" and not keys and not image and p.id != subject:
            continue
        kept.append((p, kind, keys))
    ids = {p.id for p, _, _ in kept}
    out = []
    for p, kind, keys in kept:
        target = _target(p, kind, r.edges, ids, subject) if kind != "post" else None
        key = next((k for k in keys if k in subject_keys), None) or (keys[0] if keys else None)
        m = p.metrics
        out.append(
            Sighting(
                id=p.id,
                platform=p.platform,
                account=account_key(p),
                at=p.created_at,
                time_exact=p.created_at is not None and p.time_source != "search_hint",
                kind=kind,
                target=target,
                text=_own_text(p),
                url=p.url,
                media_key=key,
                likes=m.likes if m else None,
                reposts=m.reposts if m else None,
                replies=m.replies if m else None,
            )
        )
    return out


def _date(s: str | None) -> datetime | None:
    """A join date at least to the day ("2021-10-23"); month-only dates (an X profile page read
    by Oxylabs: "2009-06") are left out: as the 1st of the month they would make accounts that
    joined weeks apart look created on the same day."""
    if not s or len(s) < 10:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _x_avatar(url: str | None) -> bool | None:
    if not url:
        return None  # not read, not "no avatar"
    return "default_profile" not in url


def _fill(info: AccountInfo, **fields: Any) -> AccountInfo:
    """Fields still missing on `info` taken from `fields`."""
    upd = {k: v for k, v in fields.items() if v is not None and getattr(info, k) is None}
    return info.model_copy(update=upd) if upd else info


def _from_account(a: Account) -> dict:
    bsky = a.platform == "bluesky"
    return dict(
        handle=a.handle,
        name=a.name,
        created_at=_date(a.joined),
        followers=a.followers,
        following=a.following,
        posts_count=a.posts_count,
        bio=a.bio,
        # a Bluesky profile without "avatar" has none; an X page that showed none: unknown
        has_avatar=bool(a.avatar_url) if bsky else _x_avatar(a.avatar_url),
    )


def _from_fx(p: dict) -> dict:
    return dict(
        handle=p.get("handle"),
        name=p.get("name"),
        created_at=p.get("joined"),
        followers=p.get("followers"),
        following=p.get("following"),
        posts_count=p.get("posts_count"),
        bio=p.get("bio"),
        has_avatar=_x_avatar(p.get("avatar_url")),
    )


def _from_bsky(pr: dict) -> dict:
    created = pr.get("createdAt")
    try:
        created_at = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
    except (ValueError, AttributeError):
        created_at = None
    return dict(
        handle=pr.get("handle"),
        name=pr.get("displayName"),
        created_at=created_at,
        followers=pr.get("followersCount"),
        following=pr.get("followsCount"),
        posts_count=pr.get("postsCount"),
        bio=pr.get("description"),
        has_avatar=bool(pr.get("avatar")),
    )


def build_accounts(
    r: LookupResult,
    sightings: list[Sighting],
    fx_authors: dict[str, dict] | None = None,
    bsky_profiles: list[dict] | None = None,
) -> dict[str, AccountInfo]:
    """What the lookup knows about the accounts behind `sightings`, by Sighting.account.

    X: the profile FxTwitter sent with each post read through it (exact join time, counts, bio,
    avatar), filled in from a profile page read (`result.accounts`). Bluesky: the profile read
    (`result.accounts`), and `bsky_profiles` (getProfiles). Accounts with nothing known are
    left out."""
    keys = {s.account for s in sightings}
    platform = {s.account: s.platform for s in sightings}
    # (platform, handle lowercased) -> account key, from the posts
    by_handle: dict[tuple[str, str], str] = {}
    for p in r.posts:
        if p.author_handle and (k := account_key(p)) in keys:
            by_handle[(p.platform, p.author_handle.lower())] = k
    out: dict[str, AccountInfo] = {}

    def add(key: str, fields: dict) -> None:
        info = out.get(key) or AccountInfo(key=key, platform=platform[key])
        out[key] = _fill(info, **fields)

    for handle, prof in (fx_authors or {}).items():
        if (k := f"x:{handle.lower()}") in keys:
            add(k, _from_fx(prof))
    for a in r.accounts:
        if k := by_handle.get((a.platform, a.handle.lower())):
            add(k, _from_account(a))
    for pr in bsky_profiles or []:
        k = pr.get("did") if pr.get("did") in keys else None
        k = k or by_handle.get(("bluesky", (pr.get("handle") or "").lower()))
        if k:
            add(k, _from_bsky(pr))
    return out


def basis_note(n: int) -> str:
    return f"{n} post{'' if n == 1 else 's'} our search found"


# ------------------------------------------------------------------ the live index, shared
class LiveBackoff:
    """The shared live index (swarm.live.LiveSwarmIndex) for every lookup, stepping aside for
    `retry_s` after a query that failed or came too late: a database that doesn't answer costs
    one lookup's attempt, not one per lookup. The index itself answers None for a DB error, so a
    late answer is how a hanging database shows."""

    def __init__(
        self,
        index: LiveSwarms,
        retry_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.index, self.retry_s, self._clock = index, retry_s, clock
        self._down_until: float | None = None
        self._lock = threading.Lock()

    def down(self) -> bool:
        with self._lock:
            return self._down_until is not None and self._clock() < self._down_until

    def failed(self) -> None:
        with self._lock:
            self._down_until = self._clock() + self.retry_s

    def find_by_cids(self, cids: list[str]) -> SwarmDetail | None:
        return self.index.find_by_cids(cids)

    def find_by_picture(self, image: bytes) -> SwarmDetail | None:
        return self.index.find_by_picture(image)


def _usable(index: Any) -> Any:
    """The live index, or None while it is backing off (LiveBackoff.down)."""
    down = getattr(index, "down", None)
    return None if index is None or (callable(down) and down()) else index


def _live_failed(index: Any) -> None:
    failed = getattr(index, "failed", None)
    if callable(failed):
        failed()


# ------------------------------------------------------------------ the step
@dataclass
class _Input:
    result: LookupResult  # a copy: the step never writes into it
    fx_authors: dict[str, dict]
    picture: bytes | None  # the uploaded image, or the post's picture when the lookup holds it
    quoting: set[str] = field(default_factory=set)  # X posts read with a quoted post


@dataclass
class _Sample:
    report: SwarmReport | None = None
    failed: bool = False  # the scorer raised
    receipts: list[dict] = field(default_factory=list)  # Lookup.receipt kwargs


@dataclass
class _Live:
    detail: SwarmDetail | None = None
    failed: bool = False  # a query raised


def _skip_reason(lk: Lookup) -> str | None:
    deps, r = lk.deps, lk.result
    if deps.swarm_scorer is None and deps.live_swarms is None:
        return "not configured"
    if r.kind == "image":
        return None
    if r.kind in ("x_post", "bsky_post"):
        subj = next((p for p in r.posts if p.id == r.subject_id), None)
        return None if has_picture(subj) else "no picture"
    return "no picture"


def _picture(lk: Lookup) -> bytes | None:
    """Picture bytes the lookup already holds: the upload, or the submitted post's own picture
    it downloaded to read (none on an image-reading cache hit). Nothing is downloaded for this."""
    if lk._upload is not None:
        return lk._upload
    subj = next((p for p in lk.result.posts if p.id == lk.result.subject_id), None)
    urls = set(subj.media_urls) if subj else set()
    held = [(url, data) for url, _h, _m, data in lk._shots] + list(lk._pictures.items())
    return next((data for url, data in held if url in urls and data), None)


def live_cids(r: LookupResult) -> list[str]:
    """The blob CIDs the live index is asked by: the submitted Bluesky post's own picture files
    only (a post of the same file has the same CID). Never a reply's or a text match's picture:
    those may be another picture, and the index would answer with that picture's group."""
    subj = next((p for p in r.posts if p.id == r.subject_id), None)
    if subj is None or subj.platform != "bluesky":
        return []
    return list(dict.fromkeys(c for u in subj.media_urls if (c := bsky_cid(u))))


def _profiles(lk: Lookup, sightings: list[Sighting], known: dict, out: _Sample, end: float):
    """getProfiles for the Bluesky accounts the lookup has no profile of: at most 2 batched
    calls, only while the lookup's provider time lasts (the reading's reserve stays free)."""
    bsky = lk.deps.bsky
    if bsky is None or not hasattr(bsky, "profiles"):
        return []
    dids = []
    for s in sightings:
        if s.platform == "bluesky" and s.account.startswith("did:") and s.account not in dids:
            if s.account not in known or known[s.account].followers is None:
                dids.append(s.account)
    got: list[dict] = []
    for i in range(0, min(len(dids), PROFILES_PER_CALL * PROFILES_CALLS), PROFILES_PER_CALL):
        batch = dids[i : i + PROFILES_PER_CALL]
        try:
            lk._spend(need_s=1.0)  # BudgetExceeded: the reading's time isn't spent on this
            deadline = min(end - 0.4, time.monotonic() + PROFILES_CAP_S, lk._provider_deadline())
            if deadline - time.monotonic() < 1.0:
                break
            profiles = bsky.profiles(batch, deadline=deadline)
        except Exception as exc:  # budget, time or the AppView: the step goes on without them
            log.info("lookup %s swarm: getProfiles not read: %s", lk.result.id, exc)
            if type(exc).__name__ not in ("BudgetExceeded", "BlueskyTimeUp"):
                out.receipts.append(
                    dict(
                        tool="bluesky",
                        endpoint="getProfiles",
                        summary=f"Bluesky profiles for the swarm check not read ({len(batch)})",
                        call=dict(getattr(bsky, "last_call", {}) or {}),
                        ok=False,
                    )
                )
            break
        got += [p for p in profiles if isinstance(p, dict)]
        out.receipts.append(
            dict(
                tool="bluesky",
                endpoint="getProfiles",
                summary=f"Bluesky profiles read for the swarm check: {len(profiles)} of "
                f"{len(batch)} accounts (joined, followers, posts, bio, avatar)",
                call=dict(getattr(bsky, "last_call", {}) or {}),
            )
        )
    return got


def _sample(lk: Lookup, inp: _Input, sightings: list[Sighting], end: float) -> _Sample:
    """The search sample's report (getProfiles first, when there is time)."""
    out = _Sample()
    accounts = build_accounts(inp.result, sightings, inp.fx_authors)
    extra = _profiles(lk, sightings, accounts, out, end)
    if extra:
        accounts = build_accounts(inp.result, sightings, inp.fx_authors, extra)
    try:
        rep = lk.deps.swarm_scorer(
            sightings, accounts, basis="sample", basis_note=basis_note(len(sightings))
        )
        out.report = rep if isinstance(rep, SwarmReport) else SwarmReport.model_validate(rep)
    except Exception as exc:
        log.warning("lookup %s swarm: scorer failed: %s", inp.result.id, exc, exc_info=True)
        out.failed = True
    return out


def _ask_live(index: Any, cids: list[str], picture: bytes | None, lookup_id: str) -> _Live:
    """The live Bluesky group of this picture: by the submitted post's blob CIDs, else by the
    picture's perceptual hash."""
    out = _Live()
    calls: list[tuple[str, Callable[[], Any]]] = []
    if cids:
        calls.append(("cids", lambda: index.find_by_cids(cids)))
    if picture:
        calls.append(("picture", lambda: index.find_by_picture(picture)))
    for name, call in calls:
        try:
            got = call()
            if got is None:
                continue
            out.detail = got if isinstance(got, SwarmDetail) else SwarmDetail.model_validate(got)
            return out
        except Exception as exc:
            log.warning("lookup %s swarm: live index (%s) failed: %s", lookup_id, name, exc)
            out.failed = True
    return out


def choose(
    sample: SwarmReport | None, live: SwarmDetail | None
) -> tuple[SwarmReport | None, SwarmReport | None]:
    """(report shown, sample kept beside it): the live group's report when it has more posting
    accounts than the sample, else the sample's."""
    if live is not None and (sample is None or live.report.n_accounts > sample.n_accounts):
        return live.report, sample
    return sample, None


def live_ref(live: SwarmDetail | None) -> SwarmLiveRef | None:
    """What a lookup keeps of the live group: a pointer to its page (lookups are polled)."""
    if live is None:
        return None
    return SwarmLiveRef(id=live.id, verdict=live.verdict, n_accounts=live.n_accounts)


def _accounts_detail(report: SwarmReport) -> str:
    n = report.n_accounts
    live = " · live Bluesky feed" if report.basis == "live" else ""
    return f"{n} account{'' if n == 1 else 's'}{live}"


def _thread(target: Callable[[], None], name: str) -> threading.Thread:
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def run(lk: Lookup) -> None:
    """The "swarm" step. Runs after the posts are final (lookup/service.py `_run`).

    The sample and the live query run side by side, each in a thread whose late answer is
    dropped. The step ends by `min(now + SWARM_CAP_S, deadline - ASSESS_RESERVE_S)` (the primary
    reading's reserve stays free) and is skipped when less than MIN_LEFT_S is left. The sample's
    report is published as soon as it is scored; the live group (its own limit, LIVE_CAP_S)
    can only add to it."""
    from lookup.service import ASSESS_RESERVE_S

    with lk.lock:
        why = _skip_reason(lk)
        if why:
            lk.skip("swarm", why)
            return
        t0 = time.monotonic()
        end = min(t0 + SWARM_CAP_S, lk.deadline - ASSESS_RESERVE_S)
        if end - t0 < MIN_LEFT_S:
            lk.skip("swarm", "no time left")
            return
        st = lk._step("swarm")
        st.state, st.started_at = "running", datetime.now(UTC)
        inp = _Input(
            result=lk.result.model_copy(
                update={
                    "posts": [p.model_copy(deep=True) for p in lk.result.posts],
                    "accounts": [a.model_copy() for a in lk.result.accounts],
                    "edges": list(lk.result.edges),
                }
            ),
            fx_authors=dict(lk._fx_authors),
            picture=_picture(lk),
            quoting=set(lk._x_quoting),
        )
    lid = inp.result.id
    sightings = build_sightings(inp.result, inp.quoting)
    index = _usable(lk.deps.live_swarms)
    cids = live_cids(inp.result)
    s_box: dict[str, Any] = {}
    l_box: dict[str, Any] = {}

    def sample_work() -> None:
        try:
            s_box["out"] = _sample(lk, inp, sightings, end)
        except Exception as exc:  # handed to the step below
            s_box["error"] = exc

    def live_work() -> None:
        try:
            l_box["out"] = _ask_live(index, cids, inp.picture, lid)
        except Exception as exc:
            l_box["error"] = exc

    t_sample = (
        _thread(sample_work, "lookup-swarm")
        if sightings and lk.deps.swarm_scorer is not None
        else None
    )
    t_live = (
        _thread(live_work, "lookup-swarm-live")
        if index is not None and (cids or inp.picture)
        else None
    )
    live_end = min(t0 + LIVE_CAP_S, end)

    sample: SwarmReport | None = None
    sample_failed = False
    if t_sample is not None:
        t_sample.join(max(0.0, end - time.monotonic()))
        with lk.lock:  # the sample is shown at once; the live group can only add to it
            got: _Sample | None = s_box.get("out")
            if "error" in s_box:
                log.warning("lookup %s swarm check failed", lid, exc_info=s_box["error"])
                sample_failed = True
            elif got is not None:
                for rc in got.receipts:
                    lk.receipt(**rc)
                sample, sample_failed = got.report, got.failed
                if sample is not None:
                    lk.result.swarm = sample
                    st.state, st.detail = "done", _accounts_detail(sample)

    live: SwarmDetail | None = None
    if t_live is not None:
        t_live.join(max(0.0, live_end - time.monotonic()))
        if t_live.is_alive():
            log.warning("lookup %s swarm: live index took over %.1f s", lid, live_end - t0)
            _live_failed(index)
        elif "error" in l_box:
            _live_failed(index)
        else:
            live = l_box["out"].detail
            if live is None and l_box["out"].failed:
                _live_failed(index)

    with lk.lock:  # one critical section: a late answer is never published
        st.ended_at = datetime.now(UTC)
        shown, kept = choose(sample, live)
        if shown is None:
            if t_sample is not None and t_sample.is_alive():
                log.warning("lookup %s swarm check took over %.1f s", lid, end - t0)
                st.state, st.detail = "skipped", "took too long"
            elif sample_failed:
                st.state, st.detail = "failed", "check failed"
            else:
                st.state, st.detail = "skipped", "no posts"
            return
        lk.result.swarm, lk.result.swarm_sample = shown, kept
        lk.result.swarm_live = live_ref(live)
        st.state, st.detail = "done", _accounts_detail(shown)
