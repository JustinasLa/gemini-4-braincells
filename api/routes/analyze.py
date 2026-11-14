"""POST /api/analyze: a judge's own input.

Answers two separate questions and never mixes them up:
1. Is this account, link or text part of a cluster we monitor? Then that cluster's detector
   status and analyst verdict apply.
2. Otherwise: live web findings (Oxylabs) about the link or text. These describe where content
   appears; they are weaker evidence and are never turned into a coordination verdict.
"""

import re
from typing import Annotated
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends

from api.deps import get_checker, get_store
from api.schemas import AnalyzeRequest, AnalyzeResponse, ClusterSummary, WebFinding
from api.store import Store
from lookup.urls import classify
from signals.textnorm import normalize_url
from sources.checker import SourceChecker
from sources.links import checkable_url, claim_phrase
from sources.models import ClusterPost

router = APIRouter(prefix="/api/analyze", tags=["analyze"])

NOT_IN_WINDOW = (
    "Not seen in the clusters we monitor, so there is no coordination verdict. "
    "Coordination is a property of many accounts acting together; one post or link alone "
    "cannot show it. The web findings below only describe where the content appears."
)

AUTHOR_CONTEXT = (
    "This post is not in a cluster we monitor, but its author is a member of {which}. "
    "That is context about the account, not evidence that this post was part of coordinated "
    "activity."
)


_AT_POST = re.compile(r"^at://([^/\s]+)/app\.bsky\.feed\.post/([^/?#\s]+)/?$", re.I)
_AT_ACTOR = re.compile(r"^at://([^/\s]+)/?$", re.I)
_BSKY_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:bsky\.app|staging\.bsky\.app)/", re.I)

NOT_LOOKED_UP = (
    "This link is not looked up: only Bluesky post and profile links, at:// post URIs and "
    "public web pages are."
)


def bsky_target(url: str) -> tuple[str, str | None] | None:
    """(author, rkey) for a Bluesky post link or at:// post URI; (author, None) for a profile
    link (`bsky.app/profile/<x>` only, not its feeds or lists). The author is a DID or a handle,
    as written. The scheme may be missing, as in links copied from a share sheet. None otherwise.
    """
    url = url.strip()
    if m := _AT_POST.match(url):
        return unquote(m[1]), m[2]
    if m := _AT_ACTOR.match(url):
        return unquote(m[1]), None
    if not _BSKY_LINK.match(url):
        return None
    full = url if re.match(r"^https?://", url, re.I) else f"https://{url}"
    target = classify(full)
    if target is None or not target.handle:
        return None
    author = unquote(target.handle).removeprefix("@")
    if target.kind == "bsky_post":
        return author, target.post_id
    if target.kind == "bsky_profile" and len([s for s in urlsplit(full).path.split("/") if s]) == 2:
        return author, None  # exactly /profile/<x>: a feed or list link says nothing about posts
    return None


@router.post("", response_model=AnalyzeResponse)
def analyze(
    req: AnalyzeRequest,
    store: Annotated[Store, Depends(get_store)],
    checker: Annotated[SourceChecker | None, Depends(get_checker)],
) -> AnalyzeResponse:
    clusters: dict[int, ClusterSummary] = {}
    notes: list[str] = []
    handle = (req.handle or "").strip().removeprefix("@")
    url = (req.url or "").strip()
    text = (req.text or "").strip()

    bsky = bsky_target(url) if url else None
    handles = {handle} if handle else set()
    if bsky and bsky[1] is None:  # a profile link is the same question as the handle field
        handles.add(bsky[0])

    for h in sorted(handles):
        clusters.update((c.id, c) for c in store.clusters_for_handle(h))
    if url:
        variants = sorted({u for u in (url, normalize_url(url)) if u})  # stored normalized
        clusters.update((c.id, c) for c in store.clusters_for_url(variants))
    phrase = claim_phrase([text]) if text else None
    if phrase:  # short text would match unrelated posts, so it is not looked up
        clusters.update((c.id, c) for c in store.clusters_for_text(phrase))
    author_clusters: list[ClusterSummary] = []
    if bsky and bsky[1]:
        author, rkey = bsky
        in_post = store.clusters_for_post(author, rkey)
        clusters.update((c.id, c) for c in in_post)
        if not in_post:  # the author's clusters are context, not a verdict on this post
            author_clusters = sorted(
                (c for c in store.clusters_for_handle(author) if c.id not in clusters),
                key=lambda c: c.window_end,
                reverse=True,
            )
            if author_clusters:
                ids = ", ".join(f"#{c.id}" for c in author_clusters)
                which = f"cluster{'s' if len(author_clusters) > 1 else ''} {ids}"
                notes.append(AUTHOR_CONTEXT.format(which=which))

    # Bluesky links point back into Bluesky, not at an outside source: never sent to the web check.
    web_url = url if url and not bsky and checkable_url(url) else None
    if url and not bsky and not web_url:  # file:, localhost, media files, Bluesky feeds/lists
        notes.append(NOT_LOOKED_UP)
    findings: list[WebFinding] = []
    if (web_url or phrase) and checker is None:
        notes.append("Live web checks are not configured on this server.")
    elif web_url or phrase:
        # One synthetic post lets the cluster checker run on ad-hoc input; adhoc=True makes it
        # describe the input ("Your text ...") instead of a cluster and its accounts.
        post = ClusterPost(did="adhoc", text=text or None, embed_urls=[web_url] if web_url else [])
        findings = [WebFinding(**e.model_dump()) for e in checker.check([post], adhoc=True)]
        if not findings:
            notes.append("The live web check returned nothing usable for this input.")
    elif text:
        notes.append(
            "Text too short for a reliable exact-phrase web search "
            "(needs 5+ words, or 10+ characters in Japanese, Chinese or Thai)."
        )

    if clusters:
        return AnalyzeResponse(
            status="matched",
            clusters=sorted(clusters.values(), key=lambda c: c.window_end, reverse=True),
            author_clusters=author_clusters,
            web_findings=findings,
            notes=notes,
        )
    return AnalyzeResponse(
        status="insufficient",
        author_clusters=author_clusters,
        web_findings=findings,
        notes=[NOT_IN_WINDOW, *notes],
    )
