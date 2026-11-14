"""Screenshot receipts for clusters: a screenshot of a post is a claim, so check it.

For every media finding that Nemotron read as a screenshot of a post (`detail.screenshot`, a
`ScreenshotPost` as a dict), `lookup.receipts.find_original` looks for the real post and
compares. Each check becomes one `kind='media'` evidence row per cluster, keyed by
`detail.media_hash = "screenshot-receipt:<hash>"` (like `TEMPLATE_KEY` for the OCR template),
so its id stays stable for analyst citations.

- The summary describes the screenshot, never a person.
- Every fetch the check made is kept in `detail.fetches` (receipts, like the rest of lookup).
- A row checked less than `recheck_s` ago is reused without any network call.
- The checks of one cluster run in parallel on a private pool, all under one time budget, so a
  cluster never holds the worker much longer than `budget_s`.

Without `detail.screenshot` on the findings (before the screenshot read is deployed) this
produces nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from lookup import receipts as lookup_receipts
from lookup.models import ScreenshotReceipt
from lookup.receipts import Budget, ReceiptDeps, ScreenshotPost

log = logging.getLogger(__name__)

KEY_PREFIX = "screenshot-receipt:"
ROW_TYPE = "screenshot_receipt"
DEFAULT_BUDGET_S = 20.0
RECHECK_S = 24 * 3600.0
GRACE_S = 2.0  # how long past the budget we wait for a check before giving up on it
MAX_WORKERS = 4
ENV_FLAG = "MEDIA_SCREENSHOT_RECEIPTS"

DepsFactory = Callable[[Callable[..., int], datetime], ReceiptDeps]


def receipt_key(media_hash: str) -> str:
    return f"{KEY_PREFIX}{media_hash}"


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """On unless MEDIA_SCREENSHOT_RECEIPTS is 0/false/no/off."""
    return env.get(ENV_FLAG, "1").strip().lower() not in ("0", "false", "no", "off")


# --- the screenshot read ---------------------------------------------------------------------


def shot_from_finding(finding) -> ScreenshotPost | None:
    """The screenshot read on a finding (media/worker.py Finding.screenshot, or the stored
    detail.screenshot), or None when the image is not a screenshot of a post."""
    shot = getattr(finding, "screenshot", None)
    if shot is not None:
        return shot
    raw = finding.detail().get("screenshot")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return ScreenshotPost.from_detail(raw)
    except TypeError as exc:  # a contract change we don't know yet: skip, don't break the worker
        log.warning("unreadable detail.screenshot hash=%s: %s", finding.group.media_hash, exc)
        return None


def _read_fingerprint(shot: ScreenshotPost) -> str:
    """Changes when the read itself changes (a new prompt read the same file differently)."""
    key = shot.to_detail()
    key.pop("latency_ms")  # request timing doesn't change what the screenshot shows
    data = json.dumps(key, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha1(data).hexdigest()[:16]


# --- receipts the check issued ---------------------------------------------------------------


class FetchLog:
    """A ReceiptSink that keeps every fetch as a JSON dict (ids are 1-based, per check)."""

    def __init__(self, now: Callable[[], datetime]):
        self._now, self._lock = now, threading.Lock()
        self.fetches: list[dict[str, Any]] = []

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
    ) -> int:
        call = call or {}
        with self._lock:
            rid = len(self.fetches) + 1
            self.fetches.append(
                {
                    "id": rid,
                    "tool": tool,
                    "endpoint": endpoint,
                    "summary": str(summary)[:400],
                    "source_url": source_url,
                    "query": query,
                    "request_id": call.get("request_id"),
                    "latency_ms": latency_ms if latency_ms is not None else call.get("latency_ms"),
                    "ok": ok,
                    "at": self._now().isoformat(),
                }
            )
            return rid


# --- wording ---------------------------------------------------------------------------------


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def receipt_summary(r: ScreenshotReceipt) -> str:
    """One plain sentence about the screenshot (never about a person)."""
    where = lookup_receipts._NAMES.get(r.platform)
    handle = _clip(r.handle.lstrip("@"), 60) if r.handle else None
    if handle:
        head = f"Screenshot of @{handle}'s post" + (f" on {where}" if where else "")
    else:
        head = "Screenshot of a post" + (f" on {where}" if where else "")
    if r.label == "matches":
        return f"{head}: matches the real post" + (
            f" ({r.original_url})." if r.original_url else "."
        )
    if r.label == "differs":
        first = _clip(r.differences[0], 200).rstrip(".") if r.differences else "the words differ"
        return f"{head}: differs from the real post: {first}."
    if r.label == "not_found":
        return f"{head}: {lookup_receipts.NOT_FOUND}."
    reason = _clip(r.reason, 200).rstrip(".") if r.reason else "no reason recorded"
    return f"{head}: not checked: {reason}."


def _unchecked(shot: ScreenshotPost, media_url: str | None, reason: str) -> ScreenshotReceipt:
    receipt = lookup_receipts._unchecked(shot, reason)
    receipt.media_url = media_url
    return receipt


def _row(
    finding,
    shot,
    receipt: ScreenshotReceipt,
    fetches,
    checked_at: datetime,
    budget_s,
    carrier_at: str | None = None,
):
    group = finding.group
    detail_post_urls = finding.detail().get("post_urls") or []
    found = receipt.label in ("matches", "differs") and receipt.original_url
    return {
        "summary": receipt_summary(receipt),
        "source_url": receipt.original_url if found else finding.source_url(),
        "model": shot.model or receipt.read_model,
        "detail": {
            "type": ROW_TYPE,
            "media_hash": receipt_key(group.media_hash),
            "source_media_hash": group.media_hash,
            "media_url": finding.media_url or receipt.media_url,
            "post_uris": group.post_uris[:50],
            "post_urls": detail_post_urls[:50],
            "n_posts": len(group.post_uris),
            "n_accounts": len(group.dids),
            "label": receipt.label,
            "read": _read_fingerprint(shot),
            "carrier_at": carrier_at,  # the earliest carrier post: the check's time anchor
            "receipt": receipt.model_dump(mode="json"),
            "fetches": fetches,
            "checked_at": checked_at.isoformat(),
            "budget_s": budget_s,
            "model": shot.model,
        },
    }


def _reused(finding, existing_row: dict[str, Any]) -> dict[str, Any] | None:
    """The stored row as a row dict: its answer, receipt, fetches and checked_at as stored, the
    posts carrying the screenshot as they are now (a new carrier gets the check too). With the
    same carriers the detail is unchanged, so the upsert is a no-op."""
    group = finding.group
    stored = existing_row["detail"]
    carriers = {
        "post_uris": group.post_uris[:50],
        "post_urls": (finding.detail().get("post_urls") or [])[:50],
        "n_posts": len(group.post_uris),
        "n_accounts": len(group.dids),
    }
    detail = (
        stored if all(stored.get(k) == v for k, v in carriers.items()) else {**stored, **carriers}
    )
    try:
        receipt = ScreenshotReceipt.model_validate(detail.get("receipt") or {})
    except ValueError:
        return None
    found = receipt.label in ("matches", "differs") and receipt.original_url
    return {
        "summary": receipt_summary(receipt),
        "source_url": receipt.original_url if found else finding.source_url(),
        "model": detail.get("model") or receipt.read_model,
        "detail": detail,
    }


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _same_check(detail: dict[str, Any], finding, shot, carrier_at: str | None) -> bool:
    """Same file, same read and same earliest carrier post: the stored answer still holds. An
    earlier carrier moves the time anchor (a screenshot can't show a later post)."""
    return (
        detail.get("type") == ROW_TYPE
        and detail.get("source_media_hash") == finding.group.media_hash
        and detail.get("read") == _read_fingerprint(shot)
        and detail.get("carrier_at") == carrier_at
    )


def _carrier_at(finding, created_at: Mapping[str, datetime | None]) -> datetime | None:
    """The earliest known creation time among the posts carrying the screenshot."""
    times = [t for t in (created_at.get(u) for u in finding.group.post_uris) if t is not None]
    return min(times) if times else None


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t is not None else None


# --- the checks ------------------------------------------------------------------------------


def receipt_rows(
    findings: Iterable,
    deps_factory: DepsFactory,
    *,
    existing: Mapping[str, dict[str, Any]] | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
    recheck_s: float = RECHECK_S,
    created_at: Mapping[str, datetime | None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    workers: int = MAX_WORKERS,
) -> list[dict[str, Any]]:
    """One screenshot_receipt evidence row per finding that carries a screenshot read.

    `existing` is `store.stored_media()` ({detail.media_hash: {id, detail}}): a row for the
    same file and read, checked less than `recheck_s` ago, is reused with no calls. The rest
    are checked in parallel under one shared budget of `budget_s`. `created_at` maps post uri
    -> its created_at: the earliest post carrying the screenshot is the check's time anchor.
    `deps_factory(sink, anchor)` builds the ReceiptDeps for one check.
    """
    existing = existing or {}
    created_at = created_at or {}
    started = now()
    out: dict[str, dict[str, Any]] = {}
    todo: list[tuple[Any, ScreenshotPost, dict[str, Any] | None]] = []
    seen: set[str] = set()
    carrier: dict[str, str | None] = {}
    for f in findings:
        shot = shot_from_finding(f)
        key = receipt_key(f.group.media_hash)
        if shot is None or key in seen:
            continue
        seen.add(key)
        carrier[key] = _iso(_carrier_at(f, created_at))
        old = existing.get(key)
        if old is not None and _same_check(old["detail"], f, shot, carrier[key]):
            at = _parse_time(old["detail"].get("checked_at"))
            if at is not None and (started - at).total_seconds() < recheck_s:
                row = _reused(f, old)
                if row is not None:
                    out[key] = row
                    continue
        todo.append((f, shot, old))
    if not todo:
        return list(out.values())

    budget = Budget.seconds(budget_s)  # one deadline for the whole cluster: checks run at once

    def check(f, shot) -> tuple[ScreenshotReceipt, list[dict[str, Any]]]:
        sink = FetchLog(now)
        first = _carrier_at(f, created_at)
        current = now()
        anchor = min(first, current) if first is not None else current
        deps = deps_factory(sink, anchor)
        try:
            receipt = lookup_receipts.find_original(shot, deps, budget)
        except Exception as exc:  # find_original should never raise; one bad check isn't fatal
            log.warning("screenshot check failed hash=%s: %s", shot.media_hash, exc)
            receipt = _unchecked(shot, f.media_url, "the check failed")
        if receipt.media_url is None and f.media_url:
            receipt = receipt.model_copy(update={"media_url": f.media_url})
        return receipt, sink.fetches

    pool = ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(todo))), thread_name_prefix="shot-receipt"
    )
    try:
        futures = [pool.submit(check, f, shot) for f, shot, _ in todo]
        wait(futures, timeout=max(0.0, budget.left()) + GRACE_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # a stuck call never holds the worker

    for (f, shot, old), fut in zip(todo, futures, strict=True):
        key = receipt_key(f.group.media_hash)
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            receipt, fetches = fut.result()
        else:
            receipt, fetches = _unchecked(shot, f.media_url, "time budget used up"), []
        if (
            receipt.label == "unchecked"
            and old is not None
            and _same_check(old["detail"], f, shot, carrier[key])
            and old["detail"].get("label") not in (None, "unchecked")
        ):
            kept = _reused(f, old)  # a failed re-check doesn't erase a real answer
            if kept is not None:
                out[key] = kept
                continue
        out[key] = _row(f, shot, receipt, fetches, now(), budget_s, carrier[key])
    return list(out.values())


# --- clients and persistence -----------------------------------------------------------------


class ReceiptClients:
    """Shared, thread-safe clients for the checks; callable as a `DepsFactory`."""

    def __init__(self, *, oxylabs: Any = None, bsky: Any = None, workers: int = 4):
        self.oxylabs, self.bsky, self.workers = oxylabs, bsky, workers

    @classmethod
    def from_settings(cls, settings) -> ReceiptClients:
        from lookup.bsky import BlueskyClient
        from sources.oxylabs import OxylabsClient

        key = getattr(settings, "oxylabs_web_api_key", "")
        return cls(oxylabs=OxylabsClient(key) if key else None, bsky=BlueskyClient())

    def __call__(self, sink, anchor: datetime) -> ReceiptDeps:
        return ReceiptDeps(
            receipt=sink, oxylabs=self.oxylabs, bsky=self.bsky, anchor=anchor, workers=self.workers
        )

    def close(self) -> None:
        for client in (self.oxylabs, self.bsky):
            if client is not None:
                client.close()

    def __enter__(self) -> ReceiptClients:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def persist_receipt_rows(
    conn, cluster_id: int, rows: list[dict[str, Any]], existing: Mapping[str, dict[str, Any]]
) -> int:
    """Upsert the rows by detail.media_hash. A re-check that changed only volatile fields
    (counts now, timings, checked_at) updates detail without bumping created_at, so it
    doesn't trigger a new verdict. Returns rows changed."""
    from media import store

    changed = 0
    for row in rows:
        old = existing.get(row["detail"]["media_hash"])
        _, did_change = store.save_media_evidence(conn, cluster_id, **row, existing=old)
        if not did_change and old is not None and old["detail"] != row["detail"]:
            store.refresh_media_detail(conn, old["id"], row["detail"])
        changed += did_change
    return changed


def post_times(conn, findings: Iterable) -> dict[str, datetime | None]:
    """created_at of the posts carrying screenshots (the checks' time anchors)."""
    from media import store

    uris = [u for f in findings if shot_from_finding(f) is not None for u in f.group.post_uris]
    return store.post_created_at(conn, uris) if uris else {}
