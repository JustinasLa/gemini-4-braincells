"""The lookup's swarm check: do the posts that carry this picture act like a swarm?

It turns what the lookup kept into the scorer's input (swarm/model.py: `Sighting`,
`AccountInfo`) and scores it (`Deps.swarm_scorer`, swarm.score.score_swarm). The posts are a
search sample, so the report says `basis: "sample"`. When the live Bluesky index
(`Deps.live_swarms`, swarm.live.LiveSwarmIndex) holds the same picture, its group (every post
our firehose saw in 24 h, already scored) is kept too, and shown when it has more posting
accounts than the sample.

Pure CPU apart from one optional batched Bluesky getProfiles call and the live index query,
all inside a hard cap (SWARM_CAP_S). It never fails the lookup and never changes `status`,
`origin_id` or anything else: the report is additional evidence about the group of posts,
never a label on one account.
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
from swarm.model import AccountInfo, Sighting, SwarmDetail, SwarmReport

if TYPE_CHECKING:
    from lookup.service import Lookup

log = logging.getLogger(__name__)

SWARM_CAP_S = 2.0  # the whole step, getProfiles and the live index included
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


def build_sightings(r: LookupResult) -> list[Sighting]:
    posts = counted_posts(r)
    ids = {p.id for p in posts}
    subject = r.subject_id if r.subject_id in ids else None
    subj = next((p for p in posts if p.id == subject), None)
    subject_keys = {k for u in (subj.media_urls if subj else []) if (k := media_key(u))}
    out = []
    for p in posts:
        kind = KIND.get(p.role, "post")  # a counted mention is a post of the picture
        target = _target(p, kind, r.edges, ids, subject) if kind != "post" else None
        keys = [k for u in p.media_urls if (k := media_key(u))]
        # a quote shows the quoted post's picture (FxTwitter lists it with the quote): that
        # file is the target's, not one this account uploaded
        key = None if kind == "quote" else next((k for k in keys if k in subject_keys), None)
        key = key or (keys[0] if keys and kind != "quote" else None)
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


# ------------------------------------------------------------------ the step
@dataclass
class _Input:
    result: LookupResult  # a copy: the step never writes into it
    fx_authors: dict[str, dict]
    picture: bytes | None  # the uploaded image, or the post's picture when the lookup holds it


@dataclass
class _Output:
    sample: SwarmReport | None = None
    live: SwarmDetail | None = None
    scorer_failed: bool = False
    receipts: list[dict] = field(
        default_factory=list
    )  # Lookup.receipt kwargs, published at the end


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
    """Picture bytes the lookup already holds: the upload, or one of the submitted post's own
    images it downloaded to read. Nothing is downloaded for this."""
    if lk._upload is not None:
        return lk._upload
    subj = next((p for p in lk.result.posts if p.id == lk.result.subject_id), None)
    urls = set(subj.media_urls) if subj else set()
    held = [(url, data) for url, _h, _m, data in lk._shots] + list(lk._pictures.items())
    return next((data for url, data in held if url in urls and data), None)


def _profiles(lk: Lookup, sightings: list[Sighting], known: dict, out: _Output, end: float):
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


def _live(lk: Lookup, sightings: list[Sighting], picture: bytes | None) -> SwarmDetail | None:
    """The live Bluesky group of this picture: by the Bluesky posts' blob CIDs, else by the
    picture's perceptual hash. Any failure: logged, nothing found."""
    index = lk.deps.live_swarms
    if index is None:
        return None
    cids: list[str] = []
    r = lk.result
    by_id = {p.id: p for p in r.posts}
    for s in sightings:
        p = by_id.get(s.id)
        if s.platform == "bluesky" and p is not None:
            cids += [c for u in p.media_urls if (c := bsky_cid(u)) and c not in cids]
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
            return got if isinstance(got, SwarmDetail) else SwarmDetail.model_validate(got)
        except Exception as exc:
            log.warning("lookup %s swarm: live index (%s) failed: %s", r.id, name, exc)
    return None


def _compute(lk: Lookup, inp: _Input, end: float) -> _Output:
    out = _Output()
    sightings = build_sightings(inp.result)
    scorer = lk.deps.swarm_scorer
    if sightings and scorer is not None:
        accounts = build_accounts(inp.result, sightings, inp.fx_authors)
        extra = _profiles(lk, sightings, accounts, out, end)
        if extra:
            accounts = build_accounts(inp.result, sightings, inp.fx_authors, extra)
        try:
            rep = scorer(sightings, accounts, basis="sample", basis_note=basis_note(len(sightings)))
            out.sample = rep if isinstance(rep, SwarmReport) else SwarmReport.model_validate(rep)
        except Exception as exc:
            log.warning("lookup %s swarm: scorer failed: %s", lk.result.id, exc, exc_info=True)
            out.scorer_failed = True
    out.live = _live(lk, sightings, inp.picture)
    return out


def choose(
    sample: SwarmReport | None, live: SwarmDetail | None
) -> tuple[SwarmReport | None, SwarmReport | None]:
    """(report shown, sample kept beside it): the live group's report when it has more posting
    accounts than the sample, else the sample's."""
    if live is not None and (sample is None or live.report.n_accounts > sample.n_accounts):
        return live.report, sample
    return sample, None


def run(lk: Lookup) -> None:
    """The "swarm" step. Runs after the posts are final (lookup/service.py `_run`)."""
    with lk.lock:
        why = _skip_reason(lk)
        if why:
            lk.skip("swarm", why)
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
        )
    end = time.monotonic() + SWARM_CAP_S
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["out"] = _compute(lk, inp, end)
        except Exception as exc:  # handed to the step below
            box["error"] = exc

    worker = threading.Thread(target=work, name="lookup-swarm", daemon=True)
    worker.start()
    worker.join(max(0.0, end - time.monotonic()))
    with lk.lock:  # one critical section: a late answer is never published
        st.ended_at = datetime.now(UTC)
        if worker.is_alive():
            log.warning("lookup %s swarm check took over %.1f s", lk.result.id, SWARM_CAP_S)
            st.state, st.detail = "skipped", "took too long"
            return
        if "error" in box:
            log.warning("lookup %s swarm check failed", lk.result.id, exc_info=box["error"])
            st.state, st.detail = "failed", "check failed"
            return
        out: _Output = box["out"]
        for rc in out.receipts:
            lk.receipt(**rc)
        shown, kept = choose(out.sample, out.live)
        if shown is None:
            st.state = "failed" if out.scorer_failed else "skipped"
            st.detail = "check failed" if out.scorer_failed else "no posts"
            return
        lk.result.swarm, lk.result.swarm_sample, lk.result.swarm_live = shown, kept, out.live
        n = shown.n_accounts
        st.state = "done"
        st.detail = f"{n} account{'' if n == 1 else 's'}" + (
            " · live Bluesky feed" if shown.basis == "live" else ""
        )
