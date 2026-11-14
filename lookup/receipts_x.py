"""X resolver for screenshot receipts: find the real X post a screenshot shows.

Phase 1 runs three Oxylabs calls in parallel (a private pool, each clamped to the budget):

1. `/search` for the screenshot's exact words on the account: `site:x.com/<h> "<phrase>"`;
2. `/search` for its keywords, unquoted, so an edited screenshot still finds the real post;
3. `/scrape` of the profile page: its recent posts carry exact ids (so exact times), and a
   recent screenshot ("2h") is usually among them.

A recent post on the profile that carries the screenshot's words (similarity >= 0.8) is already
read on X's own page and becomes a candidate directly. Otherwise phase 2 reads the best
`deps.x_read_max` posts on their own pages, in parallel. Only the author's own words are kept:
the text of a post they quote is not theirs.

Times come from the post id alone (snowflake), never from the page. Counts are what xpage read
(None when the page leaves them ambiguous). Every call becomes exactly one receipt; a provider
error becomes a failed receipt and the search goes on with what it has.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from lookup import textsim
from lookup.models import Metrics
from lookup.receipts import (
    Budget,
    Candidate,
    ReceiptDeps,
    ScreenshotPost,
    ShotTimeUp,
    same_words,
    time_fits,
)
from lookup.urls import X_STATUS_RE, snowflake_time, x_post_url
from lookup.xpage import XItem, parse_post_page, parse_profile

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
PROFILE_MATCH = 0.8  # a recent profile post this close is a candidate as read on the profile
READ_MIN_SIM = 0.35  # like lookup.service.X_MIN_SIM: a hit sharing fewer words is not about it
LOOSE_WORDS = 8
EXACT, LOOSE, PROFILE = 0, 1, 2  # how a post was found; lower wins ties
_TITLE = re.compile(r'^(.*?) on X(?:: "(.*?)(?:"(?: / X)?)?)?\s*$', re.S)


@dataclass
class _Hit:
    status_id: str
    handle: str
    score: float
    rank: int  # EXACT | LOOSE | PROFILE
    item: XItem | None = None  # set when read on the profile page
    item_score: float = 0.0  # similarity of `item`'s own text (not a search snippet's)
    receipt_id: int | None = None  # the profile scrape's receipt, for `item`


def x_handle(handle: str | None) -> str | None:
    """The handle when it is a valid X handle (a leading @ is tolerated), else None."""
    h = (handle or "").strip().removeprefix("@")
    return h if HANDLE_RE.fullmatch(h) else None


def find_x(shot: ScreenshotPost, deps: ReceiptDeps, budget: Budget) -> list[Candidate]:
    """Real X posts by `shot.handle` that may be the one in the screenshot, best first.

    An invalid X handle returns [] (no call is made; the caller words the reason). Provider
    errors never raise: they become failed receipts. Raises ShotTimeUp only when the budget
    leaves no room for any call at all.

    Worker threads only make the calls; every receipt is written here, in the caller's thread,
    before this returns. A call still running at the deadline gets a failed receipt ("no answer
    within the time budget") and can't touch the result afterwards."""
    handle = x_handle(shot.handle)
    if handle is None:
        return []
    text = shot.text or ""
    budget.check()  # nothing can start: let the caller say so

    jobs: list[_Job] = []
    if phrase := textsim.phrase(text, 12) or _short_phrase(text):
        q = f'site:x.com/{handle} "{phrase}"'
        jobs.append(
            _search_job(
                deps, q, EXACT, f"Search of @{handle}'s X posts for the screenshot's exact words"
            )
        )
    if words := _keywords(text):
        q = f"site:x.com/{handle} {' '.join(words)}"
        jobs.append(
            _search_job(
                deps, q, LOOSE, f"Search of @{handle}'s X posts for the screenshot's keywords"
            )
        )
    url = f"https://x.com/{handle}"
    jobs.append(
        _Job(
            "profile",
            url,
            None,
            lambda dl: parse_profile(deps.oxylabs.scrape_markdown(url, deadline=dl)[0], handle),
            label=f"@{handle}'s X profile",
        )
    )

    pool = ThreadPoolExecutor(max_workers=max(1, deps.workers), thread_name_prefix="receipt-x")
    try:
        if not _run(pool, jobs, deps, budget):
            raise ShotTimeUp("time budget used up")
        hits = _merge([h for j in jobs if j.done for h in _hits(j, deps, handle, text)])
        # a recent profile post with the screenshot's words, word for word (a cut-off
        # screenshot may show only its start), is a candidate as the profile shows it. One
        # that is only close may be clipped on the profile ("Show more"): it is read on its own
        # page first, and the profile's copy is the fallback when that read fails.
        cands = [
            _candidate(h.item, h.receipt_id, "profile")
            for h in hits
            if h.item is not None and same_words(text, h.item.text)
        ]
        near = {
            f"x:{h.status_id}": _candidate(h.item, h.receipt_id, "profile")
            for h in hits
            if h.item is not None
            and h.item_score >= PROFILE_MATCH
            and not same_words(text, h.item.text)
        }
        # an admitted candidate with the screenshot's words, word for word in order, at a time
        # that fits: nothing else to read
        anchor = deps.anchor or deps.now()
        exact = any(time_fits(shot.shown_time, c.created_at, anchor) for c in cands)
        if not exact:
            have = {c.id for c in cands}
            # strongest evidence first (hits are sorted by score): an exact search hit beats a
            # profile near-match; near-matches that make the cut are confirmed on their page
            to_read = [
                h
                for h in hits
                if f"x:{h.status_id}" not in have
                and (h.score >= READ_MIN_SIM or f"x:{h.status_id}" in near)
            ][: max(0, deps.x_read_max)]
            reads = [_read_job(deps, handle, h) for h in to_read]
            _run(pool, reads, deps, budget)
            for j in reads:
                if j.done and (c := _read_result(j, deps, handle)) is not None:
                    cands.append(c)
                    near.pop(c.id, None)
            cands.extend(near.values())  # read failed or wasn't possible: the profile's copy
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    rank = {f"x:{h.status_id}": h.rank for h in hits}
    out: dict[str, tuple[float, int, Candidate]] = {}
    for c in cands:
        key = (textsim.similarity(c.text, text), -rank.get(c.id, PROFILE))
        if c.id not in out or key > out[c.id][:2]:
            out[c.id] = (*key, c)
    ordered = [c for *_, c in sorted(out.values(), key=lambda v: (v[0], v[1]), reverse=True)]
    for c in ordered:
        if c.created_at is not None:
            deps.receipt(
                "x-id",
                "snowflake",
                f"Exact time from the X post id: {c.created_at.isoformat()}",
                source_url=c.url,
            )
    return ordered


# ---------------------------------------------------------------- phase helpers


@dataclass
class _Job:
    """One provider call. The worker thread sets out/call/err; the caller reads them."""

    kind: str  # search | profile | read
    url: str | None
    query: str | None
    fn: Callable[[float], Any]
    label: str = ""
    rank: int = PROFILE
    hit: _Hit | None = None
    out: Any = None
    call: dict | None = None
    err: BaseException | None = None
    done: bool = False


def _search_job(deps: ReceiptDeps, q: str, rank: int, label: str) -> _Job:
    return _Job(
        "search", None, q, lambda dl: deps.oxylabs.search(q, deadline=dl), label=label, rank=rank
    )


def _read_job(deps: ReceiptDeps, handle: str, h: _Hit) -> _Job:
    url = x_post_url(handle, h.status_id)
    return _Job(
        "read",
        url,
        None,
        lambda dl: parse_post_page(deps.oxylabs.scrape_markdown(url, deadline=dl)[0], h.status_id),
        label=f"@{handle}'s X post",
        hit=h,
    )


def _run(pool: ThreadPoolExecutor, jobs: list[_Job], deps: ReceiptDeps, budget: Budget) -> int:
    """Run jobs on `pool` until the deadline; returns how many started a call. A job still
    running at the deadline gets its failed receipt here; one that never started gets none."""
    if not jobs:
        return 0

    def go(j: _Job) -> bool:
        budget.check()  # ShotTimeUp: never started
        ox = deps.oxylabs
        before = ox.last_call
        try:
            out, err = j.fn(budget.deadline), None
        except Exception as exc:
            out, err = None, exc
        after = ox.last_call
        # a call that failed before reaching Oxylabs leaves this thread's previous call info
        j.call = dict(after) if after and (err is None or after is not before) else None
        j.out, j.err = out, err
        return True

    futures = {pool.submit(go, j): j for j in jobs}
    done, _ = wait(futures, timeout=max(0.0, budget.left()))
    started = 0
    for f, j in futures.items():
        if f in done:
            try:
                f.result()
            except ShotTimeUp:
                # never started: the lookup is incomplete, so a missing post proves nothing
                _record(deps, j, f"{j.label}: not read, time budget used up", ok=False)
                continue
            except Exception:  # a bug, not a provider error
                log.exception("receipts_x job failed")
                started += 1
                continue
            j.done = True
            started += 1
        elif f.cancel():  # still queued at the deadline: cancelled, never started
            _record(deps, j, f"{j.label}: not read, time budget used up", ok=False)
        else:  # running, or finished just after the wait: too late either way
            started += 1
            _record(deps, j, f"{j.label}: no answer within the time budget", ok=False)
    return started


def _record(deps: ReceiptDeps, j: _Job, summary: str, *, ok: bool = True) -> int:
    return deps.receipt(
        "oxylabs",
        "/search" if j.kind == "search" else "/scrape",
        summary,
        source_url=j.url,
        query=j.query,
        call=j.call if j.done else None,
        ok=ok,
    )


def _hits(j: _Job, deps: ReceiptDeps, handle: str, text: str) -> list[_Hit]:
    """Receipt for a finished phase-1 job and the hits it found."""
    if j.err is not None:
        why = "failed" if j.kind == "search" else "not read"
        _record(deps, j, f"{j.label} {why}: {_why(j.err)}", ok=False)
        return []
    if j.kind == "search":
        hits = j.out or []
        _record(deps, j, f"{j.label}: {len(hits)} result{'s' * (len(hits) != 1)}")
        out = []
        for hit in hits:
            m = X_STATUS_RE.match(hit.url)
            if not m or m.group(1).lower() != handle.lower():
                continue  # another account's post, or not a post
            own = _title_text(hit.title)
            snippet = textsim.strip_snippet(hit.snippets[0]) if hit.snippets else ""
            score = max(
                textsim.similarity(own, text, truncated=True) if own else 0.0,
                textsim.similarity(snippet, text, truncated=True) if snippet else 0.0,
            )
            if score < READ_MIN_SIM and any(same_words(text, t) for t in (own, snippet) if t):
                # a cut-off screenshot that the hit's text continues word for word: worth
                # reading (the page's author and words are checked after the read)
                score = READ_MIN_SIM
            out.append(_Hit(m.group(2), m.group(1), score, j.rank))
        return out
    p = j.out
    readable = bool(p.recent) or p.followers is not None or p.joined is not None
    summary = (
        f"@{handle}'s X profile read: {len(p.recent)} recent post{'s' * (len(p.recent) != 1)}"
        if readable
        else f"@{handle}'s X profile page read, but nothing on it is visible logged out"
    )
    rid = _record(deps, j, summary, ok=readable)
    out = []
    for it in p.recent:  # xpage keeps only the account's own posts (not reposts), with ids
        sim = textsim.similarity(it.text, text)
        out.append(_Hit(it.status_id, it.handle, sim, PROFILE, it, sim, rid))
    return out


def _read_result(j: _Job, deps: ReceiptDeps, handle: str) -> Candidate | None:
    page = j.out
    if j.err is not None or page.main is None:
        why = (
            f"not read: {_why(j.err)}"
            if j.err is not None
            else "page read, but the post was not on it (deleted, protected or age-restricted)"
        )
        _record(deps, j, f"@{handle}'s X post {why}", ok=False)
        return None
    m = page.main
    if m.handle.lower() != handle.lower():
        # the id resolves under another account: not the post the screenshot shows
        _record(deps, j, f"X post read: it is by @{m.handle}, not @{handle}")
        return None
    rid = _record(deps, j, f"@{m.handle}'s X post read: {_short(m.text)}")
    return _candidate(m, rid, "/scrape" if j.hit and j.hit.rank == PROFILE else "/search")


def _merge(hits: list[_Hit]) -> list[_Hit]:
    """One hit per status id: best score, best rank, the profile item if any. Best first."""
    by_id: dict[str, _Hit] = {}
    for h in hits:
        cur = by_id.get(h.status_id)
        if cur is None:
            by_id[h.status_id] = _Hit(**vars(h))
            continue
        cur.score = max(cur.score, h.score)
        cur.rank = min(cur.rank, h.rank)
        if h.item is not None:
            cur.item, cur.item_score, cur.receipt_id = h.item, h.item_score, h.receipt_id
    return sorted(by_id.values(), key=lambda h: (-h.score, h.rank))


def _candidate(it: XItem, rid: int | None, source: str) -> Candidate:
    """`it.text` is the author's own words: xpage keeps a quoted post's text apart."""
    created = snowflake_time(it.status_id)
    return Candidate(
        id=f"x:{it.status_id}",
        platform="x",
        url=x_post_url(it.handle, it.status_id),
        handle=it.handle,
        author_name=it.name,
        text=it.text,
        created_at=created,
        time_source="snowflake" if created is not None else None,
        metrics=Metrics(
            views=it.views,
            replies=it.replies,
            reposts=it.reposts,
            likes=it.likes,
            bookmarks=it.bookmarks,
        ),
        receipt_id=rid,
        source=source,
        edited=it.edited,
    )


# ---------------------------------------------------------------- small helpers


def _short_phrase(text: str) -> str | None:
    """A short post ("This is it.") is searched as it is, quoted: too short for
    textsim.phrase and, being stop words, without keywords."""
    t = " ".join(textsim.strip_snippet(text.replace('"', " ")).split()).strip(" .,:;!?…")
    words = [w for w in t.split() if not w.startswith(("http://", "https://"))]
    return " ".join(words) if 0 < len(words) <= 12 else None


def _keywords(text: str) -> list[str]:
    """The first distinct keywords (no stop words, URLs or punctuation) for a loose search."""
    return list(dict.fromkeys(textsim.tokens(text)))[:LOOSE_WORDS]


def _title_text(title: str) -> str | None:
    """'Name on X: "text..."' -> 'text' (the search engine's copy of the post's words)."""
    m = _TITLE.match(title or "")
    if not m:
        return None
    return (m.group(2) or "").strip().removesuffix("...").removesuffix("…").strip() or None


def _short(text: str | None, n: int = 60) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _why(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:160]
