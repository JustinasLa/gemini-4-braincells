"""POST /api/analyze: a judge's own input.

Answers two separate questions and never mixes them up:
1. Is this account, link or text part of a cluster we monitor? Then that cluster's detector
   status and analyst verdict apply.
2. Otherwise: live web findings (Oxylabs) about the link or text. These describe where content
   appears; they are weaker evidence and are never turned into a coordination verdict.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import get_checker, get_store
from api.schemas import AnalyzeRequest, AnalyzeResponse, ClusterSummary, WebFinding
from api.store import Store
from signals.textnorm import normalize_url
from sources.checker import SourceChecker
from sources.links import claim_phrase
from sources.models import ClusterPost

router = APIRouter(prefix="/api/analyze", tags=["analyze"])

NOT_IN_WINDOW = (
    "Not seen in the clusters we monitor, so there is no coordination verdict. "
    "Coordination is a property of many accounts acting together; one post or link alone "
    "cannot show it. The web findings below only describe where the content appears."
)


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

    if handle:
        clusters.update((c.id, c) for c in store.clusters_for_handle(handle))
    if url:
        variants = sorted({u for u in (url, normalize_url(url)) if u})  # stored normalized
        clusters.update((c.id, c) for c in store.clusters_for_url(variants))
    phrase = claim_phrase([text]) if text else None
    if phrase:  # short text would match unrelated posts, so it is not looked up
        clusters.update((c.id, c) for c in store.clusters_for_text(phrase))

    findings: list[WebFinding] = []
    if (url or phrase) and checker is None:
        notes.append("Live web checks are not configured on this server.")
    elif url or phrase:
        # One synthetic post lets the cluster checker run unchanged on ad-hoc input.
        post = ClusterPost(did="adhoc", text=text or None, embed_urls=[url] if url else [])
        findings = [WebFinding(**e.model_dump()) for e in checker.check([post])]
        if not findings:
            notes.append("The live web check returned nothing usable for this input.")
    elif text:
        notes.append("Text too short for a reliable exact-phrase web search (needs 5+ words).")

    if clusters:
        return AnalyzeResponse(
            status="matched",
            clusters=sorted(clusters.values(), key=lambda c: c.window_end, reverse=True),
            web_findings=findings,
            notes=notes,
        )
    return AnalyzeResponse(
        status="insufficient", web_findings=findings, notes=[NOT_IN_WINDOW, *notes]
    )
