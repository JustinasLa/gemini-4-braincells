"""POST /api/analyze: a judge's own input.

Answers two separate questions and never mixes them up:
1. Is this account, link or text part of a cluster we monitor? Then that cluster's detector
   status and analyst verdict apply.
2. Otherwise: live web findings (Oxylabs) about the link or text. These describe where content
   appears; they are weaker evidence and are never turned into a coordination verdict.
"""

from typing import Annotated
from urllib.parse import unquote

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


def bsky_target(url: str) -> tuple[str, str | None] | None:
    """(author, rkey) for a Bluesky post link or at:// post URI; (author, None) for a profile.

    The author is a DID or a handle, as written in the link. None for anything else.
    """
    if url.startswith("at://"):
        parts = url.removeprefix("at://").split("/")
        if len(parts) == 1 and parts[0]:
            return unquote(parts[0]), None
        if len(parts) >= 3 and parts[0] and parts[1] == "app.bsky.feed.post" and parts[2]:
            return unquote(parts[0]), parts[2]
        return None
    target = classify(url)
    if target is None or target.kind not in ("bsky_post", "bsky_profile") or not target.handle:
        return None
    return unquote(target.handle).removeprefix("@"), target.post_id


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
            author_clusters = [c for c in store.clusters_for_handle(author) if c.id not in clusters]
            if author_clusters:
                ids = ", ".join(f"#{c.id}" for c in author_clusters)
                which = f"cluster{'s' if len(author_clusters) > 1 else ''} {ids}"
                notes.append(AUTHOR_CONTEXT.format(which=which))

    # Bluesky links point back into Bluesky, not at an outside source: never sent to the web check.
    web_url = url if url and not bsky and checkable_url(url) else None
    findings: list[WebFinding] = []
    if (web_url or phrase) and checker is None:
        notes.append("Live web checks are not configured on this server.")
    elif web_url or phrase:
        # One synthetic post lets the cluster checker run unchanged on ad-hoc input.
        post = ClusterPost(did="adhoc", text=text or None, embed_urls=[web_url] if web_url else [])
        findings = [WebFinding(**e.model_dump()) for e in checker.check([post])]
        if not findings:
            notes.append("The live web check returned nothing usable for this input.")
    elif text:
        notes.append("Text too short for a reliable exact-phrase web search (needs 5+ words).")

    if clusters:
        return AnalyzeResponse(
            status="matched",
            clusters=sorted(clusters.values(), key=lambda c: c.window_end, reverse=True),
            author_clusters=sorted(author_clusters, key=lambda c: c.window_end, reverse=True),
            web_findings=findings,
            notes=notes,
        )
    return AnalyzeResponse(
        status="insufficient",
        author_clusters=sorted(author_clusters, key=lambda c: c.window_end, reverse=True),
        web_findings=findings,
        notes=[NOT_IN_WINDOW, *notes],
    )
