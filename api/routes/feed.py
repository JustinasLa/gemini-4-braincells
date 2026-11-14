"""Atom feed of the newest clusters, so anyone can follow new calls in a feed reader.

GET /api/feed.atom: by default the detector's `coordinated` clusters, real traffic only (no
synthetic eval clusters), newest first. Each entry links to the cluster's dashboard page. Entries
describe what a group of accounts did together; they never name an account.
"""

import re
from datetime import UTC, datetime
from typing import Annotated
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Query, Request, Response

from api.deps import get_store
from api.schemas import ClusterSummary, Status
from api.store import Store

router = APIRouter(prefix="/api", tags=["feed"])

ATOM_NS = "http://www.w3.org/2005/Atom"
# Same words as the dashboard (web/public/index.html, STATUS).
LABEL = {
    "coordinated": "Coordinated",
    "insufficient": "Can’t tell",
    "likely_organic": "Likely organic",
}
FEED_TITLE = {
    "coordinated": "Noise Radar: new coordinated clusters",
    "insufficient": "Noise Radar: new can’t-tell clusters",
    "likely_organic": "Noise Radar: new likely-organic clusters",
}
ID_PREFIX = "tag:noise-radar,2026-11-13:"
DISCLAIMER = (
    "A cluster is evidence of coordinated behaviour, not proof of who is behind it or why. "
    "It is never a claim about one account or a viewpoint."
)

# XML 1.0 forbids most control characters; detector and model text can carry them.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _clean(text: object) -> str:
    return _XML_BAD.sub("", str(text))


def public_base(request: Request) -> str:
    """Scheme and host the visitor used. Behind Traefik and Caddy the API sees plain HTTP from
    Caddy, so take X-Forwarded-Proto/-Host when present (Caddy passes Traefik's through)."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    proto = proto if proto in ("http", "https") else request.url.scheme
    host = host or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _num(x: float | None, fmt: str) -> str | None:
    return None if x is None else format(x, fmt)


def entry_title(c: ClusterSummary) -> str:
    size = f"{c.size} accounts" if c.size else "Accounts"
    fired = f", {c.families_fired} signal families" if c.families_fired else ""
    return f"{LABEL.get(c.status, c.status)}: {size} acting together{fired} (cluster #{c.id})"


def entry_text(c: ClusterSummary) -> str:
    lines: list[str] = []
    if c.verdict and c.verdict.summary:
        lines.append(c.verdict.summary.strip())
    sigs = [s for s in c.top_signals if s.summary]
    if sigs:
        lines.append("Signals:")
        lines += [f"- {s.summary.strip()}" for s in sigs]
    facts = [
        f"coordination score {_num(c.score, '.2f')}",
        f"timing p-value {_num(c.p_value, '.3g')}" if c.p_value is not None else None,
        f"{c.window_kind} window" if c.window_kind else None,
        f"{c.window_start:%Y-%m-%d %H:%M}–{c.window_end:%H:%M} UTC",
    ]
    lines.append("; ".join(f for f in facts if f) + ".")
    if c.verdict and c.verdict.agrees_with_detector is False and c.verdict.llm_assessment:
        lines.append(
            f"Gemini reads it differently ({LABEL.get(c.verdict.llm_assessment)}); "
            "the detector's call stands."
        )
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _updated(c: ClusterSummary) -> datetime:
    t = c.updated_at or c.window_end
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
    if text is not None:
        el.text = _clean(text)
    return el


def build_feed(
    clusters: list[ClusterSummary], *, base: str, self_url: str, status: str, now: datetime
) -> bytes:
    ET.register_namespace("", ATOM_NS)
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    _sub(feed, "id", f"{ID_PREFIX}feed/{status}")
    _sub(feed, "title", FEED_TITLE.get(status, f"Noise Radar: new {status} clusters"))
    _sub(
        feed,
        "subtitle",
        "Groups of Bluesky accounts acting together, as called by the Noise Radar detector. "
        "Each entry shows the cluster's current call; calls near the threshold can change. "
        + DISCLAIMER,
    )
    _sub(feed, "updated", _iso(max((_updated(c) for c in clusters), default=now)))
    _sub(feed, "link", rel="self", type="application/atom+xml", href=self_url)
    _sub(feed, "link", rel="alternate", type="text/html", href=f"{base}/")
    author = _sub(feed, "author")
    _sub(author, "name", "Noise Radar")
    _sub(feed, "generator", "Noise Radar API")

    for c in clusters:
        entry = _sub(feed, "entry")
        _sub(entry, "id", f"{ID_PREFIX}cluster/{c.id}")
        _sub(entry, "title", entry_title(c))
        _sub(entry, "updated", _iso(_updated(c)))
        _sub(entry, "published", _iso(c.window_end))
        _sub(entry, "link", rel="alternate", type="text/html", href=f"{base}/#/cluster/{c.id}")
        _sub(entry, "category", term=c.status, label=LABEL.get(c.status, c.status))
        _sub(entry, "summary", entry_text(c), type="text")
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


@router.get(
    "/feed.atom",
    response_class=Response,
    responses={200: {"content": {"application/atom+xml": {}}}},
)
def clusters_feed(
    request: Request,
    store: Annotated[Store, Depends(get_store)],
    status: Status = "coordinated",
    min_score: Annotated[float, Query(ge=0, le=1)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_synthetic: bool = False,
) -> Response:
    """Atom 1.0 feed of the newest clusters with one detector status (default `coordinated`)."""
    clusters = store.list_clusters(
        status=status, min_score=min_score, limit=limit, include_synthetic=include_synthetic
    )
    base = public_base(request)
    query = f"?{request.url.query}" if request.url.query else ""
    body = build_feed(
        clusters,
        base=base,
        self_url=f"{base}{request.url.path}{query}",
        status=status,
        now=datetime.now(UTC),
    )
    return Response(
        body,
        media_type="application/atom+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )
