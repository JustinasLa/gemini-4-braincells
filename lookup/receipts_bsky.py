"""Screenshot receipts, Bluesky: find the real post a Bluesky screenshot shows.

Three public AppView calls run in parallel, each clamped to the check's budget:

- `resolveHandle`  the handle -> its DID (a 400 means no account has that handle now);
- `searchPosts`    `from:<handle> <the screenshot's words>` (unquoted, so a lightly edited
                   text still hits);
- `getAuthorFeed`  the author's recent posts, for when search misses (the search index lags
                   and drops some posts).

Only posts by the screenshot's account are kept: author DID == the resolved DID, or, when the
handle could not be resolved, author handle == the handle. Every call that ran is one receipt.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx

from lookup import textsim
from lookup.bsky import BlueskyTimeUp, parse_time, post_url
from lookup.models import Metrics
from lookup.receipts import Budget, Candidate, ReceiptDeps, ScreenshotPost, ShotTimeUp

log = logging.getLogger(__name__)

TOOL = "bluesky"
SEARCH_LIMIT = 25
FEED_LIMIT = 50
# Screenshots are often of replies (a reply dunk, a reply that went viral), so the feed
# includes the author's replies; the default posts_no_replies would hide them.
FEED_FILTER = "posts_with_replies"
REPOST = "app.bsky.feed.defs#reasonRepost"  # a pinned post (#reasonPin) is still the author's


def normalise_handle(handle: str) -> str:
    """'@Ada' -> 'ada.bsky.social'; '@ada.example.com' -> 'ada.example.com'.
    Bluesky handles are domains: a bare name is a bsky.social handle."""
    h = handle.strip().lstrip("@").strip().lower()
    return h if "." in h else f"{h}.bsky.social"


def search_words(text: str | None) -> str | None:
    """The words to search for: textsim.phrase, else the first words of a short text."""
    p = textsim.phrase(text, 8)
    if p:
        return p
    words = [
        w.replace('"', "")
        for w in (text or "").split()
        if not w.startswith(("@", "#", "http://", "https://"))
    ]
    out = " ".join(w for w in words[:8] if w).strip(" .,:;")
    return out or None


@dataclass
class _Call:
    endpoint: str
    run: Any  # () -> result
    query: str | None = None
    source_url: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Written under `lock`; frozen once `closed` is set (the caller stopped waiting): a queued
    # call then never starts and a late answer is dropped (its receipt says "no answer").
    closed: bool = False
    started_at: float | None = None
    sent: bool = False  # a request went out (not gated by the budget)
    done: bool = False
    result: Any = None
    error: BaseException | None = None
    call: dict = field(default_factory=dict)


def _work(c: _Call, client: Any, budget: Budget) -> None:
    with c.lock:
        if c.closed:
            return
        c.started_at = time.monotonic()
    result, error = None, None
    try:
        budget.check()
        with c.lock:
            if c.closed:
                return
            c.sent = True
        result = c.run()
    except (ShotTimeUp, BlueskyTimeUp):  # gated before any request went out
        with c.lock:
            if not c.closed:
                c.sent = False
        return
    except Exception as e:  # noqa: BLE001 - a provider error becomes a failed receipt
        error = e
    call = dict(client.last_call)  # thread-local: this call's own status and latency
    with c.lock:
        if not c.closed:
            c.result, c.error, c.call, c.done = result, error, call, True


def _err(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def _status(e: BaseException | None) -> int | None:
    return e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None


def _candidate(pv: dict, rid: int | None, source: str) -> Candidate:
    author = pv.get("author") or {}
    rec = pv.get("record") or {}
    return Candidate(
        id=pv["uri"],
        platform="bluesky",
        url=post_url(pv["uri"], author.get("handle")),
        handle=author.get("handle"),
        author_name=author.get("displayName") or None,
        text=rec.get("text") or "",
        created_at=parse_time(rec.get("createdAt")),
        time_source="api",
        metrics=Metrics(
            replies=pv.get("replyCount"),
            reposts=pv.get("repostCount"),
            likes=pv.get("likeCount"),
            quotes=pv.get("quoteCount"),
        ),
        receipt_id=rid,
        source=source,
    )


def find_bsky(shot: ScreenshotPost, deps: ReceiptDeps, budget: Budget) -> list[Candidate]:
    """Candidate real posts for a Bluesky screenshot (searchPosts hits first, then the feed).

    Precondition: `shot.handle` is set and `deps.bsky` is a BlueskyClient. Provider errors
    become failed receipts, never exceptions. Raises ShotTimeUp only when no call could start;
    calls still running when the budget ends are recorded as unanswered."""
    if not shot.handle:
        raise ValueError("find_bsky needs shot.handle")
    if deps.bsky is None:
        raise ValueError("Bluesky client not configured")
    client = deps.bsky
    handle = normalise_handle(shot.handle)
    profile = f"https://bsky.app/profile/{handle}"
    words = search_words(shot.text)

    calls = [
        _Call(
            "resolveHandle",
            lambda: client.resolve_did(handle, deadline=budget.deadline),
            source_url=profile,
        )
    ]
    if words:  # no legible words: the feed alone is the source
        q = f"from:{handle} {words}"
        calls.append(
            _Call(
                "searchPosts",
                lambda: client.search(q, limit=SEARCH_LIMIT, deadline=budget.deadline),
                query=q,
                source_url=f"https://bsky.app/search?q={quote_plus(q)}",
            )
        )
    calls.append(
        _Call(
            "getAuthorFeed",
            lambda: client.author_feed_items(
                handle, limit=FEED_LIMIT, deadline=budget.deadline, filter=FEED_FILTER
            ),
            source_url=profile,
        )
    )

    pool = ThreadPoolExecutor(max_workers=max(1, deps.workers), thread_name_prefix="shot-bsky")
    try:
        futures = []
        for c in calls:
            try:
                budget.check()
            except ShotTimeUp:
                continue
            futures.append(pool.submit(_work, c, client, budget))
        if futures:
            wait_futures(futures, timeout=max(0.0, budget.left()))
    finally:
        for c in calls:
            with c.lock:
                c.closed = True
        pool.shutdown(wait=False, cancel_futures=True)

    live = [c for c in calls if c.sent]  # frozen now: every flag was set under its lock
    if not live:
        raise ShotTimeUp("time budget used up before any Bluesky call could start")
    for c in calls:
        if not c.sent:  # the budget kept it from starting: the lookup is incomplete
            deps.receipt(
                TOOL,
                c.endpoint,
                f"Bluesky {c.endpoint} for @{handle}: not read, time budget used up",
                source_url=c.source_url,
                query=c.query,
                ok=False,
            )
    return _collect(handle, live, deps)


def _collect(handle: str, live: list[_Call], deps: ReceiptDeps) -> list[Candidate]:
    rids: dict[str, int] = {}
    did: str | None = None
    handle_gone = False
    at = f"@{handle}"

    def record(c: _Call, summary: str, ok: bool) -> int:
        rid = deps.receipt(
            TOOL,
            c.endpoint,
            summary,
            source_url=c.source_url,
            query=c.query,
            call=c.call or None,
            ok=ok,
        )
        rids[c.endpoint] = rid
        return rid

    def unanswered(c: _Call) -> bool:
        """Sent but still running when the budget ended: a failed receipt."""
        if c.done:
            return False
        elapsed = time.monotonic() - (c.started_at or time.monotonic())
        c.call = {"endpoint": c.endpoint, "status": None, "latency_ms": int(elapsed * 1000)}
        return True

    for c in live:
        if c.endpoint != "resolveHandle":
            continue
        if unanswered(c):
            record(c, f"Bluesky handle {at} not resolved: no answer within the time budget", False)
        elif c.error is not None:
            if _status(c.error) == 400:
                handle_gone = True
                # an answer, not a failure: no account has this handle now
                record(c, f"Bluesky handle {at} not found on Bluesky (no account has it now)", True)
            else:
                record(c, f"Bluesky handle {at} not resolved: {_err(c.error)}", False)
        else:
            did = c.result
            record(c, f"Bluesky handle resolved: {at} -> {did}", True)

    def mine(pv: dict) -> bool:
        a = pv.get("author") or {}
        if did:
            return a.get("did") == did
        return (a.get("handle") or "").lower() == handle

    found: dict[str, list[tuple[dict, str]]] = {"searchPosts": [], "getAuthorFeed": []}
    for c in live:
        if c.endpoint == "searchPosts":
            if unanswered(c):
                record(
                    c, f"Bluesky search of {at}'s posts: no answer within the time budget", False
                )
            elif c.error is not None:
                record(c, f"Bluesky search of {at}'s posts failed: {_err(c.error)}", False)
            else:
                posts = [pv for pv in c.result or [] if pv.get("uri")]
                own = [pv for pv in posts if mine(pv)]
                extra = f" ({len(own)} by {at})" if len(own) != len(posts) else ""
                record(
                    c,
                    f"Bluesky search of {at}'s posts for the screenshot's words: "
                    f"{_n(len(posts), 'post')}{extra}",
                    True,
                )
                found["searchPosts"] = [(pv, "searchPosts") for pv in own]
        elif c.endpoint == "getAuthorFeed":
            if unanswered(c):
                record(c, f"Bluesky author feed of {at}: no answer within the time budget", False)
            elif c.error is not None:
                why = (
                    "profile not found (HTTP 400)"
                    if _status(c.error) == 400
                    else f"failed: {_err(c.error)}"
                )
                # HTTP 400 is the AppView's answer "no such profile", not a failed fetch
                record(c, f"Bluesky author feed of {at} {why}", _status(c.error) == 400)
            else:
                items = [f for f in c.result or [] if (f.get("post") or {}).get("uri")]
                own, reposts = [], 0
                for f in items:
                    if (f.get("reason") or {}).get("$type") == REPOST:
                        reposts += 1  # someone's post shared, not the author's own
                    elif mine(f["post"]):
                        own.append(f["post"])
                skipped = f" ({_n(reposts, 'repost')} skipped)" if reposts else ""
                record(
                    c,
                    f"Bluesky author feed of {at}: {_n(len(items), 'recent post')}{skipped}",
                    True,
                )
                found["getAuthorFeed"] = [(pv, "getAuthorFeed") for pv in own]

    if handle_gone:  # no account has this handle, so nothing found can be by it
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    for endpoint in ("searchPosts", "getAuthorFeed"):
        for pv, source in found[endpoint]:
            if pv["uri"] in seen:
                continue
            seen.add(pv["uri"])
            out.append(_candidate(pv, rids.get(endpoint), source))
    log.info(
        "screenshot bsky handle=%s did=%s candidates=%d calls=%s",
        handle,
        did,
        len(out),
        ",".join(c.endpoint for c in live),
    )
    return out


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'' if k == 1 else 's'}"
