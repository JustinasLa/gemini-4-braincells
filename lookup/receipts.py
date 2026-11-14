"""Screenshot receipts: a screenshot of a post is a claim. Find the real post and check it.

Input: what Nemotron read from the screenshot (`media.screenshot.ScreenshotPost`). Output: a
`ScreenshotReceipt` (lookup/models.py) labelled

- `matches`   the real post was found and says what the screenshot shows (link to it);
- `differs`   the real post was found but its words (or its time) differ (link + word diff);
- `not_found` no matching post found: it may be deleted, private or never posted;
- `unchecked` the screenshot can't be checked (no legible handle, platform we can't read,
              time budget used up), with the reason.

The label describes the screenshot, never a person. Counts are context only (posts gain and
lose likes); they never decide the label. Every fetch becomes a Receipt via `deps.receipt`,
the same as the rest of lookup. Independent calls run in parallel, each clamped to `budget`.

Resolvers: Bluesky in lookup/receipts_bsky.py, X in lookup/receipts_x.py.
"""

from __future__ import annotations

import difflib
import importlib
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol

from lookup.models import DiffPart, Metrics, ScreenshotReceipt
from lookup.textsim import _QUOTES, similarity
from media.screenshot import ScreenshotPost

log = logging.getLogger(__name__)

NOT_FOUND = "no matching post found: it may be deleted, private or never posted"
COUNTS_NOTE = "Counts change as people like and repost; shown as context, never as a verdict."
SUPPORTED = ("x", "bluesky")
_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")  # what X allows (the @ is stripped first)


class ShotTimeUp(RuntimeError):
    """The time budget ran out before a call could start."""


class ReceiptSink(Protocol):
    """Same signature as `lookup.service.Lookup.receipt`; returns the new receipt id."""

    def __call__(
        self,
        tool: str,
        endpoint: str,
        summary: str,
        *,
        source_url: str | None = None,
        query: str | None = None,
        call: dict | None = None,
        ok: bool = True,
        latency_ms: int | None = None,
    ) -> int: ...


@dataclass
class Budget:
    """An absolute end (time.monotonic()) for every call this check makes."""

    deadline: float

    @classmethod
    def seconds(cls, s: float) -> Budget:
        return cls(time.monotonic() + s)

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def check(self, need_s: float = 3.0) -> None:
        """Gate before every provider call: raise ShotTimeUp when `need_s` no longer fits."""
        if self.left() < need_s:
            raise ShotTimeUp("time budget used up")


@dataclass
class ReceiptDeps:
    receipt: ReceiptSink
    oxylabs: Any = None  # sources.oxylabs.OxylabsClient (search, scrape_markdown); thread-safe
    bsky: Any = None  # lookup.bsky.BlueskyClient
    # the latest moment the screenshot can have been taken: the time the post carrying it was
    # created, else the check's own time. Relative times ("2h") count back from it.
    anchor: datetime | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    workers: int = 4  # parallel calls per check (a private pool: never the caller's pool)
    x_read_max: int = 2  # X candidate posts read on their own page


@dataclass
class Candidate:
    """A real post that may be the one in the screenshot, read on its own page or API."""

    id: str  # "x:<status id>" or the at:// uri, as lookup Post ids
    platform: str  # x | bluesky
    url: str
    handle: str | None
    author_name: str | None
    text: str
    created_at: datetime | None
    time_source: str | None  # snowflake | api
    metrics: Metrics | None
    receipt_id: int | None
    source: str = ""  # how it was found: searchPosts | author_feed | /search | profile ...


MIN_SIMILARITY = 0.5  # below this no candidate is taken to be the post in the screenshot
_NAMES = {
    "x": "X",
    "bluesky": "Bluesky",
    "threads": "Threads",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}


def _name(platform: str | None) -> str:
    p = (platform or "").strip().lower()
    return _NAMES.get(p, p or "an unknown platform")


def find_original(shot: ScreenshotPost, deps: ReceiptDeps, budget: Budget) -> ScreenshotReceipt:
    """Find the real post the screenshot shows and compare. Never raises: a failure or an
    exhausted budget becomes an `unchecked` receipt with the reason."""
    t0 = time.monotonic()
    ids: list[int] = []
    failed: list[int] = []  # receipts of fetches that failed or ran out of time
    closed = False  # set when the check returns: later receipts are dropped
    lock = threading.Lock()

    def done(r: ScreenshotReceipt) -> ScreenshotReceipt:
        nonlocal closed
        with lock:
            closed = True
            r.receipt_ids = list(ids)
        r.latency_ms = int((time.monotonic() - t0) * 1000)
        return r

    try:
        anchor = _utc(deps.anchor or deps.now())
        platform = (shot.platform or "unknown").strip().lower()
        handle = (shot.handle or "").strip().lstrip("@") or None
        if platform in SUPPORTED:
            targets = [platform]
        elif platform in ("unknown", "other", ""):
            if not handle:
                return done(
                    _unchecked(
                        shot,
                        "The screenshot's platform isn't clear, so the "
                        "real post can't be looked up.",
                    )
                )
            targets = list(SUPPORTED)  # a handle but no platform: try both, keep the better
        else:
            return done(
                _unchecked(
                    shot,
                    "We can only look up X and Bluesky posts; this "
                    f"screenshot looks like {_name(platform)}.",
                )
            )
        if not handle:
            return done(
                _unchecked(
                    shot,
                    "No account handle is legible in the screenshot, so "
                    "the real post can't be looked up.",
                )
            )
        if not comparable_text(shot.text):
            return done(
                _unchecked(
                    shot,
                    "No post text is legible in the screenshot, so there is nothing to compare "
                    "the real post with.",
                )
            )
        if "x" in targets and len(targets) > 1 and not _X_HANDLE.fullmatch(handle):
            targets = [t for t in targets if t != "x"]  # can't be an X account: not an X post
        ready = [t for t in targets if _client(deps, t) is not None]
        unavailable = [t for t in targets if t not in ready]
        if not ready:
            what = " and ".join(_name(t) for t in targets)
            return done(
                _unchecked(shot, f"Looking up {what} posts is not configured on this server.")
            )
        if ready == ["x"] and not _X_HANDLE.fullmatch(handle):
            return done(
                _unchecked(
                    shot,
                    f"The handle read from the screenshot (@{handle}) isn't a valid X handle, "
                    "so the real post can't be looked up.",
                )
            )

        def receipt(*args: Any, **kwargs: Any) -> int:
            with lock:
                if closed:  # a straggler after the check returned: not part of it
                    return 0
                rid = deps.receipt(*args, **kwargs)
                ids.append(rid)
                if kwargs.get("ok", True) is False:
                    failed.append(rid)
            return rid

        sub = replace(deps, receipt=receipt, anchor=anchor)
        results: list[list[Candidate] | BaseException] = []
        if len(ready) == 1:
            results.append(_resolve(ready[0], shot, sub, budget))
        else:
            with ThreadPoolExecutor(max_workers=len(ready)) as pool:
                results = list(pool.map(lambda t: _resolve(t, shot, sub, budget), ready))
        candidates = [c for r in results if isinstance(r, list) for c in r]
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors and not candidates:
            if any(isinstance(e, ShotTimeUp) for e in errors):
                return done(
                    _unchecked(shot, "time budget used up before the real post was looked up")
                )
            e = errors[0]
            return done(_unchecked(shot, f"lookup failed: {type(e).__name__}"))
        with lock:
            closed = True
            # a resolver that raised (or ran out of time) left its part of the search undone
            rids, n_failed = list(ids), len(failed) + len(errors)
        tried = max(len(rids), n_failed)
        r = judge(shot, candidates, anchor, receipt_ids=rids)
        if unavailable and r.label == "not_found":
            # a platform it may be on wasn't searched: a missing post proves nothing
            what = " and ".join(_name(t) for t in unavailable)
            r.label = "unchecked"
            r.reason = (
                f"No matching post found on {' and '.join(_name(t) for t in ready)}, and looking "
                f"up {what} posts is not configured on this server, so the real post could not "
                "be checked."
            )
        elif n_failed and r.label == "not_found":
            # a search that didn't finish proves nothing about whether the post exists
            r.label = "unchecked"
            r.reason = (
                f"{n_failed} of {tried} lookups failed or ran out of time, so the real post "
                "could not be checked; a missing post here proves nothing."
            )
        elif n_failed and r.label == "differs":
            r.differences.append(
                f"{n_failed} of {tried} lookups failed or ran out of time, so a closer "
                "post may exist that wasn't read."
            )
        return done(r)
    except Exception as e:  # never raises: the receipt says what went wrong
        log.warning("screenshot check failed: %r", e, exc_info=True)
        return done(_unchecked(shot, f"lookup failed: {type(e).__name__}"))


def _client(deps: ReceiptDeps, platform: str) -> Any:
    return deps.bsky if platform == "bluesky" else deps.oxylabs


def _resolve(
    platform: str, shot: ScreenshotPost, deps: ReceiptDeps, budget: Budget
) -> list[Candidate] | BaseException:
    """Run one resolver (imported lazily); its exception is returned, not raised."""
    try:
        budget.check(0.0)
        if platform == "bluesky":
            fn = importlib.import_module("lookup.receipts_bsky").find_bsky
        else:
            fn = importlib.import_module("lookup.receipts_x").find_x
        return list(fn(shot, deps, budget) or [])
    except ShotTimeUp as e:
        return e
    except Exception as e:
        log.warning("screenshot %s lookup failed: %r", platform, e, exc_info=True)
        return e


def _base(shot: ScreenshotPost) -> ScreenshotReceipt:
    return ScreenshotReceipt(
        media_hash=shot.media_hash,
        platform=shot.platform or "unknown",
        handle=shot.handle,
        display_name=shot.display_name,
        shown_time=shot.shown_time,
        shown_text=shot.text,
        shown_counts=dict(shot.counts or {}),
        ui_oddities=list(shot.ui_oddities or []),
        read_confidence=shot.confidence,
        read_model=shot.model,
    )


def _unchecked(shot: ScreenshotPost, reason: str) -> ScreenshotReceipt:
    r = _base(shot)
    r.label = "unchecked"
    r.reason = reason
    if any(v is not None for v in r.shown_counts.values()):
        r.counts_note = COUNTS_NOTE
    return r


def judge(
    shot: ScreenshotPost,
    candidates: list[Candidate],
    anchor: datetime,
    *,
    receipt_ids: list[int] | None = None,
) -> ScreenshotReceipt:
    """Pure: pick the best candidate, diff the words, check the time, set the label."""
    anchor = _utc(anchor)
    r = _base(shot)
    r.receipt_ids = list(receipt_ids or [])
    r.candidates_checked = len(candidates)
    windows = _windows(shot.shown_time, anchor)
    earliest, latest, window_note = windows[0]
    shown_counts = any(v is not None for v in r.shown_counts.values())

    def sim(c: Candidate) -> float:
        if not (shot.text or "").strip() or not (c.text or "").strip():
            return 0.0
        return max(similarity(c.text, shot.text), similarity(shot.text, c.text))

    def time_rank(c: Candidate) -> int:
        """0 the shown time fits, 1 unknown, 2 it doesn't (or the post is too new)."""
        if _after(c.created_at, anchor):
            return 2
        return {"consistent": 0, "unknown": 1}.get(_check(c.created_at, windows)[0], 2)

    truncated = _truncated(shot.text)

    def score(c: Candidate) -> float:
        s = sim(c)
        if s < MIN_SIMILARITY and same_words(shot.text, c.text):
            # the screenshot's words, word for word: a cut-off screenshot ("The bridge is
            # closed…") or a post of stop words only ("This is it.") that token similarity
            # undercounts
            s = max(similarity(shot.text, c.text, truncated=True), 1.0 if not truncated else 0.0)
            s = max(s, MIN_SIMILARITY)
        return s

    scored = [(score(c), c) for c in candidates]
    scored = [(s, c) for s, c in scored if s >= MIN_SIMILARITY]
    if not scored:
        r.label = "not_found"
        r.reason = NOT_FOUND
        r.counts_note = COUNTS_NOTE if shown_counts else None
        r.shown_time_earliest, r.shown_time_latest = earliest, latest
        return r
    far = datetime.max.replace(tzinfo=UTC)
    # fewest changed words first (word order, repeats and signs count: token-set similarity
    # can't tell "+5%" from "-5%"), then a time that fits, then similarity, then the earliest
    s, best = min(
        scored,
        key=lambda sc: (
            _changes(shot.text, sc[1].text)[1],
            time_rank(sc[1]),
            -round(sc[0], 6),
            _utc(sc[1].created_at or far),
        ),
    )

    r.original_id = best.id
    r.original_url = best.url
    r.original_text = best.text
    r.original_created_at = best.created_at
    r.original_time_source = best.time_source if best.time_source in ("snowflake", "api") else None
    r.original_receipt_id = best.receipt_id
    r.similarity = round(s, 2)
    r.counts_now = best.metrics
    real_counts = best.metrics is not None and any(
        v is not None for v in best.metrics.model_dump().values()
    )
    r.counts_note = COUNTS_NOTE if (shown_counts or real_counts) else None

    ops, shot_words, post_words = _diff_ops(shot.text, best.text)
    r.diff = _parts(ops, shot_words, post_words)
    changes, n_changed = _changes(shot.text, best.text, ops)

    r.time_check, (earliest, latest, window_note) = _check(best.created_at, windows)
    r.shown_time_earliest, r.shown_time_latest = earliest, latest
    r.time_note = _time_note(shot.shown_time, best.created_at, earliest, latest, window_note)
    if _after(best.created_at, anchor):
        # whatever the screenshot shows (or doesn't), it can't show a post made after the post
        # that carries it
        r.time_check = "inconsistent"
        r.shown_time_latest = _utc(anchor) + _AFTER_SLACK
        r.time_note = (
            f"The real post is from {_fmt(best.created_at)}, after the post that carries the "
            f"screenshot ({_fmt(anchor)}): a screenshot can't show a later post."
        )

    shot_only = [" ".join(shot_words[i1:i2]) for _, i1, i2, _, _ in changes if i2 > i1]
    post_only = [" ".join(post_words[j1:j2]) for _, _, _, j1, j2 in changes if j2 > j1]
    if shot_only:
        r.differences.append(
            "Words in the screenshot that the real post doesn't have: " + _quoted(shot_only)
        )
    if post_only:
        r.differences.append(
            "Words in the real post missing from the screenshot: " + _quoted(post_only)
        )
    if _after(best.created_at, anchor):
        r.differences.append(
            "The real post is newer than the post that carries the screenshot "
            f"({_fmt(best.created_at)})."
        )
    elif r.time_check == "inconsistent":
        r.differences.append(
            f"The time shown ('{shot.shown_time}') doesn't fit the real post's time "
            f"({_fmt(best.created_at)})."
        )

    who = (best.handle or shot.handle or "").lstrip("@")
    post = (
        f"@{who}'s real post on {_name(best.platform)}"
        if who
        else (f"the real post on {_name(best.platform)}")
    )
    if not changes and r.time_check != "inconsistent":
        r.label = "matches"
        r.reason = f"The screenshot matches {post}."
    else:
        r.label = "differs"
        what = []
        if n_changed:
            what.append(f"{n_changed} word{'s' if n_changed != 1 else ''} changed")
        if r.time_check == "inconsistent":
            what.append("the time shown doesn't fit")
        r.reason = f"The screenshot differs from {post}: {' and '.join(what)}."
    return r


def _quoted(runs: list[str]) -> str:
    return ", ".join(f"'{t}'" for t in runs)


_Window = tuple[datetime | None, datetime | None, str | None]
_FIRST_YEAR = 2006  # X (Twitter) launched in 2006: no post shown without a year is older


def _windows(shown_time: str | None, anchor: datetime) -> list[_Window]:
    """Every window the shown time allows, the latest first. One, unless the date shows no year:
    X and Bluesky leave the year out for dates in the viewer's current year, and when the
    screenshot was taken is unknown (only that it was by `anchor`), so any earlier year fits."""
    first = shown_window(shown_time, anchor)
    s = (shown_time or "").strip()
    try:
        found = _shown_date(s, _utc(anchor)) if s else None
    except ValueError:
        found = None
    if not found or not found[1]:
        return [first]
    out = [first]
    a = _utc(anchor)
    for k in range(1, max(1, a.year - _FIRST_YEAR + 1)):
        try:
            back = a.replace(year=a.year - k)
        except ValueError:  # Feb 29
            back = a.replace(year=a.year - k, day=28)
        w = shown_window(shown_time, back)
        if w[0] is not None and all(w[0] != o[0] for o in out):
            out.append(w)
    return out


def _check(
    created: datetime | None, windows: list[_Window]
) -> tuple[Literal["consistent", "inconsistent", "unknown"], _Window]:
    """The time check against every window, and the window to show (the one that fits)."""
    for w in windows:
        if _time_check(created, w[0], w[1]) == "consistent":
            return "consistent", w
    return _time_check(created, windows[0][0], windows[0][1]), windows[0]


def _time_check(
    created: datetime | None, earliest: datetime | None, latest: datetime | None
) -> Literal["consistent", "inconsistent", "unknown"]:
    if created is None or (earliest is None and latest is None):
        return "unknown"
    t = _utc(created)
    if earliest is not None and t < earliest:
        return "inconsistent"
    if latest is not None and t > latest:
        return "inconsistent"
    return "consistent"


def _time_note(
    shown: str | None,
    created: datetime | None,
    earliest: datetime | None,
    latest: datetime | None,
    window_note: str | None,
) -> str:
    if not (shown or "").strip():
        return "The screenshot shows no time, so the time wasn't compared."
    if earliest is None and latest is None:
        return f"The screenshot's time ('{shown}') isn't readable, so it wasn't compared."
    if created is None:
        return "The real post's time isn't known, so the time shown wasn't compared."
    t = _utc(created)
    head = f"The screenshot shows '{shown}'; the real post is from {_fmt(t)}"
    if earliest is not None and t < earliest:
        body = f"{head}, at least {_span(earliest - t)} earlier than the time shown allows."
    elif latest is not None and t > latest:
        body = f"{head}, at least {_span(t - latest)} later than the time shown allows."
    else:
        body = f"{head}, consistent with it."
    if window_note:
        body += f" ({window_note[0].upper()}{window_note[1:]}.)"
    return body


def _fmt(t: datetime | None) -> str:
    if t is None:
        return "an unknown time"
    t = _utc(t)
    return f"{t.day} {t:%b %Y %H:%M} UTC"


def _span(d: timedelta) -> str:
    s = max(int(d.total_seconds()), 0)
    if s >= 2 * 86400:
        return f"{s // 86400} days"
    if s >= 2 * 3600:
        return f"{s // 3600} hours"
    if s >= 120:
        return f"{s // 60} minutes"
    return f"{s} seconds"


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


# ---------------------------------------------------------------- shown time

_TZ_SLACK = timedelta(hours=14)  # UTC-12..UTC+14: conservatively 14 h either way
_REL_SLACK = timedelta(seconds=60)  # the viewer's clock may run a little fast
_TZ_NOTE = "time zone of the viewer unknown: within ±14 h"
_UNREADABLE = "shown time not readable"
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MON = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_MDY = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}})\b)?", re.I)
_DMY = re.compile(rf"(?<![\d:])\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON}\b(?:,?\s*(\d{{4}})\b)?", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])(?:\s*([ap])\.?\s?m\b\.?)?", re.I)
_REL_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 7 * 86400,
    "wk": 7 * 86400,
    "wks": 7 * 86400,
    "week": 7 * 86400,
    "weeks": 7 * 86400,
    # the shortest month / year, so the latest allowed time is never too early
    "mo": 28 * 86400,
    "mos": 28 * 86400,
    "month": 28 * 86400,
    "months": 28 * 86400,
    "y": 365 * 86400,
    "yr": 365 * 86400,
    "yrs": 365 * 86400,
    "year": 365 * 86400,
    "years": 365 * 86400,
}
_REL = re.compile(
    r"(?<![\w:.,])(\d+)\s?(" + "|".join(sorted(_REL_UNITS, key=len, reverse=True)) + r")\b",
    re.I,
)
_NOW = re.compile(r"\b(?:just\s+now|now)\b", re.I)
_UNIT_WORD = {
    1: "second",
    60: "minute",
    3600: "hour",
    86400: "day",
    7 * 86400: "week",
    28 * 86400: "month",
    365 * 86400: "year",
}


def shown_window(
    shown_time: str | None, anchor: datetime
) -> tuple[datetime | None, datetime | None, str | None]:
    """See _shown_window. "Last edited 8:56 PM · …" is when the post was last edited: it was
    created before that, so the window keeps only its latest end."""
    earliest, latest, note = _shown_window(shown_time, anchor)
    if earliest is not None and latest is not None and _EDITED.search(shown_time or ""):
        return None, latest, "the time shown is the last edit; the post was created before it"
    return earliest, latest, note


_EDITED = re.compile(r"\bedited\b", re.I)


def _shown_window(
    shown_time: str | None, anchor: datetime
) -> tuple[datetime | None, datetime | None, str | None]:
    """The real times a shown time allows: (earliest, latest, note). None = open on that side.

    Absolute times are in the viewer's unknown time zone (within about 14 h of UTC). Relative
    times ("2h") count back from `anchor`, the latest moment the screenshot can have been
    taken; how long before that it was taken is unknown, so the window is open on the early
    side. Unparseable -> (None, None, note)."""
    s = (shown_time or "").replace(" ", " ").replace(" ", " ").strip()
    if not s:
        return None, None, _UNREADABLE
    anchor = _utc(anchor)
    try:
        if found := _shown_date(s, anchor):
            day, guessed_year = found
            year_note = (
                f"; no year shown: {day:%b} {day.day} of {day.year} or an earlier year"
                if guessed_year
                else ""
            )
            midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
            if clock := _shown_clock(s):
                t = midnight.replace(hour=clock[0], minute=clock[1])
                return (
                    t - _TZ_SLACK,
                    t + _TZ_SLACK + timedelta(seconds=59),
                    _TZ_NOTE + year_note,
                )
            return (
                midnight - _TZ_SLACK,
                midnight + timedelta(days=1) + _TZ_SLACK,
                "date only, any time that day; " + _TZ_NOTE + year_note,
            )
    except ValueError:  # e.g. "Feb 30, 2026": not a real date
        return None, None, _UNREADABLE
    if m := _REL.search(s):
        n, unit = int(m.group(1)), _REL_UNITS[m.group(2).lower()]
        word = _UNIT_WORD[unit] + ("s" if n != 1 else "")
        return (
            None,
            anchor - timedelta(seconds=n * unit) + _REL_SLACK,
            f"relative time: the post was at least {n} {word} old when the screenshot was "
            "taken; when it was taken is unknown, so the post may be older",
        )
    if _NOW.search(s):
        return (
            None,
            anchor + _REL_SLACK,
            "relative time: the post was under a minute old when the screenshot was taken; "
            "when it was taken is unknown, so the post may be older",
        )
    return None, None, _UNREADABLE


def _shown_date(s: str, anchor: datetime) -> tuple[date, bool] | None:
    """(date, year_was_guessed) from the first date in `s`, else None."""
    if m := _ISO.search(s):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
    month = day = year = None
    if m := _MDY.search(s):
        month, day, year = _MONTHS[m.group(1)[:3].lower()], int(m.group(2)), m.group(3)
    elif m := _DMY.search(s):
        month, day, year = _MONTHS[m.group(2)[:3].lower()], int(m.group(1)), m.group(3)
    if month is None or day is None:
        return None
    if year:
        return date(int(year), month, day), False
    # no year: the latest such date not after the anchor's date (+1 day for time zones)
    limit = anchor.date() + timedelta(days=1)
    for y in range(limit.year, limit.year - 9, -1):
        try:
            d = date(y, month, day)
        except ValueError:  # Feb 29 in a non-leap year
            continue
        if d <= limit:
            return d, True
    raise ValueError("no such date")


def _shown_clock(s: str) -> tuple[int, int] | None:
    m = _CLOCK.search(s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap == "p" else 0)
    elif h > 23:
        return None
    return h, mi


# ---------------------------------------------------------------- word diff

_TRAIL = re.compile(r"(?:\s*(?:…|\.\.\.|show\s+more))+\s*$", re.I)
_URLISH = re.compile(r"^(?:https?://|www\.)\S*$|^[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)?$", re.I)
_SPLIT = re.compile(r"[^\s—–]+")


def _truncated(text: str | None) -> bool:
    return bool(_TRAIL.search(text or ""))


def _norm_word(w: str) -> str:
    w = w.replace("\u2212", "-").translate(_QUOTES).lower()
    if _URLISH.match(w.rstrip(".…,;:!?)\"'")):
        return ""
    i, j = 0, len(w)
    while i < j and unicodedata.category(w[i]).startswith("P") and w[i] not in "#@":
        if w[i] in "-+." and i + 1 < j and w[i + 1].isdigit():
            break  # a sign or decimal point carries meaning: "-10", "+5%", ".5"
        i += 1
    while j > i and unicodedata.category(w[j - 1]).startswith("P") and w[j - 1] not in "%‰":
        j -= 1
    w = w[i:j]
    return "" if _URLISH.match(w) else w


def _words(text: str | None) -> tuple[list[str], list[str], bool]:
    """(original words, normalised words, last word cut off by an ellipsis) for the diff."""
    t = text or ""
    cut = _TRAIL.search(t)
    glued = False
    if cut:
        # the ellipsis touches the last word ("governm…"): that word may be cut off
        glued = cut.start() > 0 and t[cut.start()] in "…."
        t = t[: cut.start()]
    orig, norm = [], []
    for w in _SPLIT.findall(t):
        n = _norm_word(w)
        if n:
            orig.append(w)
            norm.append(n)
    return orig, norm, glued and bool(norm)


def _changes(
    shot_text: str | None,
    post_text: str | None,
    ops: list[tuple[str, int, int, int, int]] | None = None,
) -> tuple[list[tuple[str, int, int, int, int]], int]:
    """The diff's changed runs and how many words changed. A cut-off screenshot ("…", "Show
    more") may miss the end of the post: that final run doesn't count."""
    if ops is None:
        ops = _diff_ops(shot_text, post_text)[0]
    changes = [op for op in ops if op[0] != "equal"]
    if _truncated(shot_text) and changes and changes[-1][0] == "insert" and changes[-1] == ops[-1]:
        changes = changes[:-1]
    return changes, sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in changes)


def time_fits(shown_time: str | None, created: datetime | None, anchor: datetime) -> bool:
    """False only when the shown time (or the carrier's time) rules the post out; unknown fits."""
    if _after(created, anchor):
        return False
    return _check(created, _windows(shown_time, anchor))[0] != "inconsistent"


# a screenshot is taken before the post carrying it; client clocks (Bluesky createdAt) may
# run a little fast, so a few minutes of slack
_AFTER_SLACK = timedelta(minutes=10)


def _after(created: datetime | None, anchor: datetime) -> bool:
    """The post was created after the latest moment the screenshot can have been taken."""
    return created is not None and _utc(created) > _utc(anchor) + _AFTER_SLACK


def same_words(shot_text: str | None, post_text: str | None) -> bool:
    """True when the post says exactly what the screenshot shows, word for word (the diff's
    normalisation: case, quote styles, edge punctuation and links don't count)."""
    if not _words(shot_text)[1]:
        return False
    return _changes(shot_text, post_text)[1] == 0


def comparable_text(text: str | None) -> bool:
    """Whether the screenshot shows any words a post could be compared against."""
    return bool(_words(text)[1])


def _diff_ops(
    shot_text: str | None, post_text: str | None
) -> tuple[list[tuple[str, int, int, int, int]], list[str], list[str]]:
    so, sn, cut_word = _words(shot_text)
    po, pn, _ = _words(post_text)
    if cut_word and len(sn) >= 1:
        # "…the governm…": the last screenshot word is cut off; it matches the real word it
        # is the start of, at the place the rest of the screenshot lines up with
        head = difflib.SequenceMatcher(None, sn[:-1], pn, autojunk=False)
        blocks = [b for b in head.get_matching_blocks() if b.size]
        k = (
            (blocks[-1].b + blocks[-1].size)
            if blocks and (blocks[-1].a + blocks[-1].size == len(sn) - 1)
            else (0 if len(sn) == 1 else -1)
        )
        if 0 <= k < len(pn) and pn[k].startswith(sn[-1]):
            sn[-1] = pn[k]
    ops = difflib.SequenceMatcher(None, sn, pn, autojunk=False).get_opcodes()
    return ops, so, po


def _parts(
    ops: list[tuple[str, int, int, int, int]], so: list[str], po: list[str]
) -> list[DiffPart]:
    parts: list[DiffPart] = []

    def add(op: str, words: list[str]) -> None:
        if not words:
            return
        if parts and parts[-1].op == op:
            parts[-1].text += " " + " ".join(words)
        else:
            parts.append(DiffPart(op=op, text=" ".join(words)))  # type: ignore[arg-type]

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            add("same", so[i1:i2])
        else:
            add("shot_only", so[i1:i2])
            add("post_only", po[j1:j2])
    return parts


def word_diff(shot_text: str | None, post_text: str | None) -> list[DiffPart]:
    """Word-level diff, ignoring case, quote styles, punctuation and URLs (screenshots
    truncate links). Runs of equal ops are merged."""
    ops, so, po = _diff_ops(shot_text, post_text)
    return _parts(ops, so, po)
