"""Value types shared by the Oxylabs client, the source checker and the API."""

from datetime import date

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippets: list[str] = Field(default_factory=list)
    position: int
    # Date the search engine shows at the start of the snippet. A hint, not a verified date.
    published_hint: date | None = None


class PageFacts(BaseModel):
    """What Oxylabs' AI extraction read off a page. Fields are None when the page lacks them."""

    url: str  # the URL we asked for
    final_url: str  # the URL the scraper ended on (after redirects)
    http_status: int | None = None
    publisher: str | None = None
    author: str | None = None
    published_at: str | None = None  # as extracted from the page, not normalised
    headline: str | None = None
    claims: list[str] = Field(default_factory=list)


class ClusterPost(BaseModel):
    did: str
    text: str | None = None
    embed_urls: list[str] = Field(default_factory=list)


class SourceEvidence(BaseModel):
    """One `evidence` row with kind='source'. `detail["key"]` identifies it for upserts."""

    summary: str
    detail: dict
    source_url: str | None = None
    model: str
