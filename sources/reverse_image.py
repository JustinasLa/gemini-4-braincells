"""Reverse image search through Oxylabs /scrape: TinEye (result JSON) and Yandex (imageview).

INTERFACE ONLY (the parsers are filled in on this branch). Probed 2026-11-14 from the prod API
container: TinEye `api/v1/result_json` and Yandex imageview work through /scrape (markdown);
Google Lens / searchbyimage (Oxylabs 500), Bing imgurl and TinEye's HTML page do not.

TinEye's crawl_date is when TinEye first saw a copy, not when it was published: it is an upper
bound on the picture's first appearance (time_source "crawl_date"). Yandex gives no dates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Engine = Literal["tineye", "yandex"]

TINEYE_CAP_S = 20.0  # observed 2-18 s
YANDEX_CAP_S = 15.0  # observed 5-13 s


class ReverseImageError(RuntimeError):
    """The engine answered, but not with something we can read (a block page, a changed
    layout, JSON without matches[]). Network/HTTP failures stay sources.oxylabs.OxylabsError."""


@dataclass(frozen=True)
class ImageHit:
    engine: Engine
    image_url: str  # the full-size copy on its own site (TinEye backlink url, Yandex link)
    thumb_url: str | None  # the engine's thumbnail (img.tineye.com / avatars.mds.yandex.net)
    page_url: str | None  # the page that shows the copy
    domain: str | None  # of page_url, else of image_url (sources.links.domain)
    first_seen: datetime | None  # tz-aware UTC midnight of the earliest backlink crawl_date
    time_source: Literal["crawl_date"] | None
    width: int | None
    height: int | None
    title: str | None  # page title (Yandex sites) or image_name (TinEye)
    rank: int  # 0-based position in the engine's own result order
    view: Literal["matches", "similar", "other_sizes", "sites"] = "matches"


@dataclass
class ReverseResult:
    engine: Engine
    query_image_url: str  # the image the engine was asked about
    request_url: str  # the engine URL that was scraped (receipt source_url)
    hits: list[ImageHit] = field(default_factory=list)
    total: int | None = None  # TinEye num_matches; None for Yandex
    earliest: datetime | None = None  # TinEye first_crawl_date (all matches, not only page 1)
    call: dict[str, Any] = field(default_factory=dict)  # OxylabsClient.last_call (receipts)


def tineye_url(image_url: str) -> str:
    raise NotImplementedError


def yandex_url(image_url: str, *, sites: bool = False) -> str:
    raise NotImplementedError


def parse_tineye(raw: str, query_image_url: str = "") -> ReverseResult:
    raise NotImplementedError


def parse_yandex(md: str, query_image_url: str = "") -> ReverseResult:
    """imageview page: "In other sizes" and "Similar images" links (view other_sizes/similar)."""
    raise NotImplementedError


def parse_yandex_sites(md: str, query_image_url: str = "") -> ReverseResult:
    """cbir_page=sites: pages containing the image (view "sites", with page_url and title)."""
    raise NotImplementedError


def search_tineye(client: Any, image_url: str, *, deadline: float | None = None) -> ReverseResult:
    """client: sources.oxylabs.OxylabsClient. deadline: time.monotonic() bound."""
    raise NotImplementedError


def search_yandex(
    client: Any, image_url: str, *, sites: bool = False, deadline: float | None = None
) -> ReverseResult:
    raise NotImplementedError
