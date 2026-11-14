"""Cluster media orchestration: candidate cluster -> unique media -> Nemotron -> evidence rows."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from api.store import bsky_post_url
from media import store
from media.blobs import BlobFetchError
from media.nemotron import MediaAnalysis, MediaInput, NemotronError, NotVisionModelError
from media.screenshot import SCREENSHOT_PROMPT_VERSION, ScreenshotPost

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClusterMedia:
    cluster_id: int
    post_uri: str
    did: str
    media_hash: str
    pds_host: str | None = None
    embed_kind: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class MediaFailure:
    media_hash: str
    did: str
    error: str
    stage: str = "analysis"
    error_code: str = "MediaError"
    media_kind: str | None = None
    post_uris: tuple[str, ...] = ()
    dids: tuple[str, ...] = ()


@dataclass
class MediaGroup:
    """Every post in one cluster that carries the same blob (same CID = same file)."""

    media_hash: str
    posts: list[ClusterMedia]

    @property
    def dids(self) -> list[str]:
        return list(dict.fromkeys(p.did for p in self.posts))

    @property
    def post_uris(self) -> list[str]:
        return list(dict.fromkeys(p.post_uri for p in self.posts))


def group_media(posts: list[ClusterMedia]) -> list[MediaGroup]:
    """Unique media, most reused first (distinct accounts, then posts, then first seen)."""
    groups: dict[str, MediaGroup] = {}
    for p in posts:
        groups.setdefault(p.media_hash, MediaGroup(p.media_hash, [])).posts.append(p)
    order = {h: i for i, h in enumerate(groups)}
    return sorted(
        groups.values(),
        key=lambda g: (-len(g.dids), -len(g.post_uris), order[g.media_hash]),
    )


# Platform names for the screenshot sentence in Finding.summary() (other/unknown: generic).
_PLATFORM_LABELS = {
    "x": "an X",
    "bluesky": "a Bluesky",
    "threads": "a Threads",
    "facebook": "a Facebook",
    "instagram": "an Instagram",
    "tiktok": "a TikTok",
}
# screenshot_status values that a cache hit reuses without a new screenshot read.
_SCREENSHOT_FINAL = ("post", "not_post", "skipped_adult")


@dataclass
class Finding:
    group: MediaGroup
    analysis: MediaAnalysis
    media_kind: str
    media_url: str
    cached: bool
    screenshot: ScreenshotPost | None = None
    # post | not_post | failed | skipped_adult; None = not a screenshot (or not read)
    screenshot_status: str | None = None
    screenshot_error: str | None = None

    @property
    def is_screenshot(self) -> bool:
        return self.analysis.media_type == "screenshot"

    def summary(self) -> str:
        a, g = self.analysis, self.group
        desc = a.description.rstrip()
        out = f"Nemotron Super VL: {desc}{'' if desc.endswith(('.', '!', '?')) else '.'}"
        if len(g.dids) > 1:
            out += f" Same file posted by {len(g.dids)} accounts ({len(g.post_uris)} posts)."
        if a.extracted_text:
            text = " / ".join(a.extracted_text)
            out += f' Text in image: "{text[:200]}{"…" if len(text) > 200 else ""}"'
        if a.manipulation_cues:
            out += f" Context cues: {'; '.join(a.manipulation_cues[:2])}."
        if self.screenshot is not None:
            out += _screenshot_sentence(self.screenshot)
        return out

    def detail(self) -> dict[str, Any]:
        urls = [u for u in (bsky_post_url(x) for x in self.group.post_uris) if u]
        return {
            **self.analysis.evidence_detail(),
            "media_kind": self.media_kind,
            "media_url": self.media_url,
            "post_uris": self.group.post_uris[:50],
            "post_urls": urls[:50],
            "dids": self.group.dids[:50],
            "profile_urls": [f"https://bsky.app/profile/{d}" for d in self.group.dids[:50]],
            "n_posts": len(self.group.post_uris),
            "n_accounts": len(self.group.dids),
            "cached": self.cached,
            **self._screenshot_detail(),
        }

    def _screenshot_detail(self) -> dict[str, Any]:
        if not self.is_screenshot:
            return {}
        out = {
            "screenshot": self.screenshot.to_detail() if self.screenshot else None,
            "screenshot_status": self.screenshot_status,
        }
        if self.screenshot_status is not None:  # which screenshot prompt made this status
            out["screenshot_prompt_version"] = SCREENSHOT_PROMPT_VERSION
        if self.screenshot_status == "failed":
            out["screenshot_error"] = self.screenshot_error
        return out

    def source_url(self) -> str | None:
        return next((u for u in map(bsky_post_url, self.group.post_uris) if u), None)


def _screenshot_sentence(post: ScreenshotPost) -> str:
    """Describes the screenshot, never the people in it."""
    platform = _PLATFORM_LABELS.get(post.platform, "a social-media")
    who = f"@{post.handle}" if post.handle else ""
    if not who and post.display_name:
        who = f'"{post.display_name}"'
    out = f" Reads as a screenshot of {platform} post{f' shown as from {who}' if who else ''}."
    if post.ui_oddities:
        out += f" Screenshot cues: {'; '.join(post.ui_oddities[:2])}."
    return out


TEMPLATE_KEY = "ocr-template"  # detail.media_hash of the cluster's cross-image template row


def _tokens(lines: list[str]) -> list[str]:
    return re.findall(r"[a-z0-9]+", " ".join(lines).lower())


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def ocr_template(
    findings: list[Finding], *, min_similarity: float = 0.5, min_tokens: int = 4
) -> dict[str, Any] | None:
    """Different files whose in-image text is mostly the same: one template, a few words swapped
    (e.g. the city in a spam ad). A blob hash can't see this; Nemotron's OCR can. Returns the
    evidence row for the largest such group spanning >= 2 images and >= 2 accounts, or None."""
    texts = {
        f.group.media_hash: (f, set(_tokens(f.analysis.extracted_text)))
        for f in findings
        if len(set(_tokens(f.analysis.extracted_text))) >= min_tokens
    }
    hashes = list(texts)
    parent = {h: h for h in hashes}

    def root(h: str) -> str:
        while parent[h] != h:
            h = parent[h]
        return h

    sims = []
    for i, a in enumerate(hashes):
        for b in hashes[i + 1 :]:
            sim = _jaccard(texts[a][1], texts[b][1])
            if sim >= min_similarity:
                parent[root(b)] = root(a)
                sims.append(sim)
    comps: dict[str, list[str]] = {}
    for h in hashes:
        comps.setdefault(root(h), []).append(h)
    members = max(comps.values(), key=len, default=[])
    group = [texts[h][0] for h in members]
    dids = list(dict.fromkeys(d for f in group for d in f.group.dids))
    if len(group) < 2 or len(dids) < 2:
        return None
    token_sets = [texts[h][1] for h in members]
    shared = set.intersection(*token_sets)
    varying = sorted(set.union(*token_sets) - shared)
    similarity = sum(
        _jaccard(x, y) for i, x in enumerate(token_sets) for y in token_sets[i + 1 :]
    ) / (len(token_sets) * (len(token_sets) - 1) / 2)
    post_uris = list(dict.fromkeys(u for f in group for u in f.group.post_uris))
    post_urls = [u for u in map(bsky_post_url, post_uris) if u]
    model = group[0].analysis.model
    summary = (
        f"Nemotron Super VL read {len(group)} different images posted by {len(dids)} accounts: "
        f"their in-image text is {round(similarity * 100)}% the same (one template"
        + (f"; words that change: {', '.join(varying[:8])}" if varying else "")
        + ")."
    )
    return {
        "summary": summary,
        "source_url": post_urls[0] if post_urls else None,
        "model": model,
        "detail": {
            "status": "complete",
            "type": "ocr_template",
            "media_hash": TEMPLATE_KEY,
            "media_hashes": sorted(members),
            "similarity": round(similarity, 3),
            "min_similarity": min_similarity,
            "shared_tokens": sorted(shared)[:60],
            "varying_tokens": varying[:60],
            "n_images": len(group),
            "n_accounts": len(dids),
            "dids": dids[:50],
            "post_uris": post_uris[:50],
            "post_urls": post_urls[:50],
            "model": model,
            "prompt_version": group[0].analysis.prompt_version,
        },
    }


@dataclass
class MediaRun:
    findings: list[Finding] = field(default_factory=list)
    failures: list[MediaFailure] = field(default_factory=list)
    skipped: int = 0  # unique media beyond max_media
    deferred: int = 0  # media skipped because it failed recently (retried after its backoff)

    @property
    def analyses(self) -> list[MediaAnalysis]:
        return [f.analysis for f in self.findings]

    def evidence_rows(self) -> list[dict[str, Any]]:
        """Evidence rows for findings, failures, and any cross-image OCR template."""
        rows = [
            {
                "summary": f.summary(),
                "detail": f.detail(),
                "source_url": f.source_url(),
                "model": f.analysis.model,
            }
            for f in self.findings
        ]
        rows.extend(
            {
                "summary": f"Media processing failed during {failure.stage}; analysis is unavailable.",
                "detail": {
                    "media_hash": failure.media_hash,
                    "status": "failed",
                    "type": "processing_failure",
                    "stage": failure.stage,
                    "error_code": failure.error_code,
                    "media_kind": failure.media_kind,
                    "post_uris": list(failure.post_uris),
                    "post_urls": [
                        url for url in map(bsky_post_url, failure.post_uris) if url
                    ],
                    "dids": list(failure.dids or (failure.did,)),
                    "profile_urls": [
                        f"https://bsky.app/profile/{did}"
                        for did in (failure.dids or (failure.did,))
                    ],
                },
                "source_url": next(
                    (url for url in map(bsky_post_url, failure.post_uris) if url), None
                ),
                "model": None,
            }
            for failure in self.failures
        )
        template = ocr_template(self.findings)
        return rows + ([template] if template else [])

    @property
    def attempted(self) -> int:
        return len(self.findings) + len(self.failures)

    @property
    def screenshot_failures(self) -> int:
        """Findings whose analysis is stored but whose screenshot read failed (retried later)."""
        return sum(f.screenshot_status == "failed" for f in self.findings)


class MediaClient(Protocol):
    def analyze(self, media: MediaInput, *, context: str | None = None) -> MediaAnalysis: ...

    def read_screenshot(self, media: MediaInput) -> ScreenshotPost | None: ...


class Fetcher(Protocol):
    def fetch(self, *, did: str, cid: str, pds_host=None, embed_kind=None) -> Any: ...


def process_cluster_media(
    posts: list[ClusterMedia],
    client: MediaClient,
    fetcher: Fetcher,
    *,
    max_media: int = 4,
    cache_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    skip: Callable[[str], bool] | None = None,
) -> MediaRun:
    """Analyze a cluster's most reused unique media.

    A blob shared by many posts is read once. `cache_lookup(hash)` returns a stored detail (any
    cluster) to reuse instead of calling the model. `skip(hash)` defers hashes that failed
    recently. Media typed "screenshot" also gets `client.read_screenshot` (once; a cached row
    whose screenshot read failed is read again). NotVisionModelError is never swallowed.
    """
    if max_media < 1:
        raise ValueError("max_media must be positive")
    run = MediaRun()
    groups = group_media(posts)
    run.skipped = max(0, len(groups) - max_media)
    for group in groups[:max_media]:
        if skip and skip(group.media_hash):
            run.deferred += 1
            continue
        first = group.posts[0]
        stored = cache_lookup(group.media_hash) if cache_lookup else None
        if stored is not None:
            try:
                analysis = MediaAnalysis.from_detail(stored)
                if analysis.media_hash != group.media_hash:
                    raise ValueError("cached media hash does not match")
            except (KeyError, TypeError, ValueError):
                log.warning(
                    "ignoring malformed cached media analysis cluster_id=%d hash=%s",
                    first.cluster_id,
                    group.media_hash,
                )
            else:
                finding = Finding(
                    group,
                    analysis,
                    stored.get("media_kind", "image"),
                    stored.get("media_url", ""),
                    cached=True,
                )
                if finding.is_screenshot:
                    reused = _stored_screenshot(stored)
                    if reused is not None:
                        finding.screenshot, finding.screenshot_status = reused
                    else:  # failed or never read: fetch again, but only for the screenshot read
                        _read_screenshot(finding, client, partial(_media_input, fetcher, group))
                run.findings.append(finding)
                continue
        try:
            fetched = _media_input(fetcher, group)
            analysis = client.analyze(fetched.input, context=first.text)
        except NotVisionModelError:
            raise
        except (BlobFetchError, NemotronError) as exc:
            run.failures.append(
                MediaFailure(
                    media_hash=group.media_hash,
                    did=first.did,
                    error=str(exc),
                    stage="fetch" if isinstance(exc, BlobFetchError) else "analysis",
                    error_code=type(exc).__name__,
                    media_kind=first.embed_kind,
                    post_uris=tuple(group.post_uris),
                    dids=tuple(group.dids),
                )
            )
            log.warning(
                "media analysis failed cluster_id=%d hash=%s error=%s",
                first.cluster_id,
                group.media_hash,
                exc,
            )
            continue
        finding = Finding(group, analysis, fetched.media_kind, fetched.url, cached=False)
        _read_screenshot(finding, client, fetched)
        run.findings.append(finding)
    return run


@dataclass(frozen=True, slots=True)
class _Fetched:
    input: MediaInput
    media_kind: str
    url: str


def _media_input(fetcher: Fetcher, group: MediaGroup) -> _Fetched:
    first = group.posts[0]
    blob = fetcher.fetch(
        did=first.did, cid=group.media_hash, pds_host=first.pds_host, embed_kind=first.embed_kind
    )
    media = MediaInput(group.media_hash, blob.mime_type, data=blob.data, source_uri=blob.url)
    return _Fetched(media, getattr(blob, "media_kind", "image"), blob.url)


def _stored_screenshot(stored: dict[str, Any]) -> tuple[ScreenshotPost | None, str] | None:
    """(post, status) from a stored screenshot row when it can be reused, else None (re-read)."""
    status, shot = stored.get("screenshot_status"), stored.get("screenshot")
    if status not in _SCREENSHOT_FINAL:
        return None
    if stored.get("screenshot_prompt_version") != SCREENSHOT_PROMPT_VERSION:
        return None  # read by an older screenshot prompt (or before this key existed)
    if status != "post":
        return None, status
    if not isinstance(shot, dict) or shot.get("prompt_version") != SCREENSHOT_PROMPT_VERSION:
        return None
    try:
        return ScreenshotPost.from_detail(shot), status
    except TypeError:  # a stored read missing contract fields: read it again
        return None


def _read_screenshot(
    finding: Finding, client: MediaClient, media: _Fetched | Callable[[], _Fetched]
) -> None:
    """Second Nemotron call for screenshots: read the post shown as fields (media/screenshot.py).

    `media` is the fetched blob, or a callable that fetches it (cache hits fetch only here).
    Adult-flagged screenshots are never sent. A failed read keeps the main analysis and marks the
    finding `failed`, so the cluster is retried; NotVisionModelError is never swallowed.
    """
    if not finding.is_screenshot:
        return
    if "adult" in finding.analysis.content_flags:
        finding.screenshot_status = "skipped_adult"
        return
    read = getattr(client, "read_screenshot", None)
    if read is None:
        return
    try:
        post = read((media() if callable(media) else media).input)
    except NotVisionModelError:
        raise
    except (BlobFetchError, NemotronError) as exc:
        finding.screenshot, finding.screenshot_status = None, "failed"
        finding.screenshot_error = str(exc)
        log.warning(
            "screenshot read failed cluster_id=%d hash=%s error=%s",
            finding.group.posts[0].cluster_id,
            finding.group.media_hash,
            exc,
        )
        return
    finding.screenshot, finding.screenshot_status = post, "post" if post else "not_post"
    finding.screenshot_error = None


def persist_evidence(conn, cluster_id: int, run: MediaRun) -> int:
    """Upsert `kind='media'` evidence, one row per detail.media_hash. Returns rows changed."""
    existing = store.stored_media(conn, cluster_id)
    changed = 0
    for row in run.evidence_rows():
        _, did_change = store.save_media_evidence(
            conn, cluster_id, **row, existing=existing.get(row["detail"]["media_hash"])
        )
        changed += did_change
    return changed


def load_cluster_media(conn, cluster_id: int) -> list[ClusterMedia]:
    return [
        ClusterMedia(
            cluster_id=cluster_id,
            post_uri=r["post_uri"],
            did=r["did"],
            media_hash=r["media_hash"],
            pds_host=r["pds_host"],
            embed_kind=r["embed_kind"],
            text=r["text"],
        )
        for r in store.cluster_media_posts(conn, cluster_id)
    ]


class FailureBackoff:
    """Remembers media that failed, so a broken blob isn't retried every pass."""

    def __init__(self, base_s: float = 300, max_s: float = 6 * 3600, clock=time.monotonic):
        self.base_s, self.max_s, self._clock = base_s, max_s, clock
        self._state: dict[str, tuple[int, float]] = {}

    def failed(self, media_hash: str) -> None:
        n = self._state.get(media_hash, (0, 0.0))[0] + 1
        self._state[media_hash] = (n, self._clock() + min(self.max_s, self.base_s * 2 ** (n - 1)))

    def __call__(self, media_hash: str) -> bool:
        state = self._state.get(media_hash)
        return state is not None and self._clock() < state[1]


class ClusterProgress:
    """Which clusters the worker can leave out of the next candidate query.

    Fully read: until signals changes the cluster (new updated_at). Incomplete (failed or
    deferred media, or a failed screenshot read): held back for `retry_s`, so they can't fill
    every batch and starve others.
    """

    def __init__(self, retry_s: float = 300, clock=time.monotonic):
        self.retry_s, self._clock = retry_s, clock
        self._read: dict[int, Any] = {}
        self._incomplete: dict[int, tuple[Any, float]] = {}

    def record(self, cluster_id: int, updated_at: Any, run: MediaRun) -> None:
        if run.failures or run.deferred or run.screenshot_failures:
            self._incomplete[cluster_id] = (updated_at, self._clock() + self.retry_s)
        else:
            self._read[cluster_id] = updated_at
            self._incomplete.pop(cluster_id, None)

    def done(self) -> dict[int, Any]:
        """{cluster_id: updated_at} to exclude now."""
        now = self._clock()
        held = {cid: at for cid, (at, until) in self._incomplete.items() if until > now}
        return {**self._read, **held}
