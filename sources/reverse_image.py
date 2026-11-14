"""Reverse image search through Oxylabs /scrape: TinEye (result JSON) and Yandex (imageview).

Probed 2026-11-14 from the prod API container: TinEye `api/v1/result_json` and Yandex
imageview work through /scrape (markdown);
Google Lens / searchbyimage (Oxylabs 500), Bing imgurl and TinEye's HTML page do not.

TinEye's crawl_date is when TinEye first saw a copy, not when it was published: it is an upper
bound on the picture's first appearance (time_source "crawl_date"). Yandex gives no dates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from typing import Any, Literal
from urllib.parse import quote, unquote_plus, urljoin, urlsplit, urlunsplit

from sources.links import domain

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
    time_source: Literal["crawl_date", "snowflake"] | None  # snowflake: lookup.imagetrace
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
    return (
        "https://tineye.com/api/v1/result_json/?url="
        f"{quote(image_url, safe='')}&sort=crawl_date&order=asc"
    )


def yandex_url(image_url: str, *, sites: bool = False) -> str:
    url = f"https://yandex.com/images/search?rpt=imageview&url={quote(image_url, safe='')}"
    return url + ("&cbir_page=sites" if sites else "")


def _crawl_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_tineye(raw: str, query_image_url: str = "") -> ReverseResult:
    raw = raw.strip()
    fence = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ReverseImageError("TinEye returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise ReverseImageError("TinEye returned no matches list")
    result = ReverseResult(
        engine="tineye",
        query_image_url=query_image_url,
        request_url=tineye_url(query_image_url),
        total=data.get("num_matches"),
        earliest=_crawl_date(data.get("first_crawl_date")),
    )
    for rank, match in enumerate(data["matches"]):
        if not isinstance(match, dict) or not isinstance(match.get("backlinks"), list):
            raise ReverseImageError("TinEye match has no backlinks list")
        backlinks = match["backlinks"]
        if not backlinks or any(not isinstance(backlink, dict) for backlink in backlinks):
            raise ReverseImageError("TinEye match has invalid backlinks")
        backlink = min(
            backlinks,
            key=lambda item: (
                _crawl_date(item.get("crawl_date")) or datetime.max.replace(tzinfo=UTC)
            ),
        )
        image_url = backlink.get("url")
        if not isinstance(image_url, str) or not image_url:
            raise ReverseImageError("TinEye backlink has no image URL")
        page_url = backlink.get("backlink") or None
        first_seen = _crawl_date(backlink.get("crawl_date"))
        result.hits.append(
            ImageHit(
                engine="tineye",
                image_url=image_url,
                thumb_url=match.get("image_url"),
                page_url=page_url,
                domain=domain(page_url or image_url),
                first_seen=first_seen,
                time_source="crawl_date" if first_seen else None,
                width=match.get("width"),
                height=match.get("height"),
                title=backlink.get("image_name"),
                rank=rank,
            )
        )
    return result


def _closing_delimiter(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 1
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _markdown_links(text: str) -> Iterator[tuple[str, str, bool]]:
    cursor = 0
    while opening := re.search(r"(!?)\[", text[cursor:]):
        start = cursor + opening.end()
        cursor = start
        label_end = _closing_delimiter(text, start, "[", "]")
        if label_end is None or text[label_end + 1 : label_end + 2] != "(":
            continue
        url_end = _closing_delimiter(text, label_end + 2, "(", ")")
        if url_end is None:
            continue
        destination = text[label_end + 2 : url_end].strip()
        url = destination.split(maxsplit=1)[0].strip("<>") if destination else ""
        yield text[start:label_end], unescape(url), bool(opening.group(1))
        cursor = url_end + 1


def _sections(md: str) -> Iterator[tuple[str, str]]:
    headings = list(re.finditer(r"(?m)^#{1,6}[ \t]+([^\n]+)", md))
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        if title.startswith("["):
            title = next(_markdown_links(title), (title, "", False))[0]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(md)
        yield title, md[heading.end() : end]


def _image_url(url: str) -> str | None:
    if not url:
        return None
    try:
        absolute = urljoin("https://yandex.com", url)
        parsed = urlsplit(absolute)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host:
            return None
        if host == "avatars.mds.yandex.net":
            return None if parsed.path.startswith("/get-images-cbir/") else absolute
        if re.search(r"(^|\.)yandex\.[a-z.]+$", host):
            return None
        return absolute
    except ValueError:
        return None


def _dimensions(label: str) -> tuple[int | None, int | None]:
    size = re.search(r"(\d+)\s*[×x]\s*(\d+)\s*$", label)
    return (int(size.group(1)), int(size.group(2))) if size else (None, None)


def _thumbnail(label: str) -> str | None:
    for _, url, is_image in _markdown_links(label):
        if is_image and (absolute := _image_url(url)):
            if urlsplit(absolute).hostname == "avatars.mds.yandex.net":
                return absolute
    return None


def _strip_tracking(url: str) -> str:
    parsed = urlsplit(url)
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if unquote_plus(part.partition("=")[0]) not in {"utm_medium", "utm_source"}
    )
    return urlunsplit(parsed._replace(query=query))


def parse_yandex(md: str, query_image_url: str = "") -> ReverseResult:
    """imageview page: "In other sizes" and "Similar images" links (view other_sizes/similar)."""
    result = ReverseResult("yandex", query_image_url, yandex_url(query_image_url))
    seen: set[str] = set()
    found_section = False
    for title, body in _sections(md):
        if title not in {"In other sizes", "Similar images"}:
            continue
        found_section = True
        for label, url, is_image in _markdown_links(body):
            image_url = _image_url(url)
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            width, height = _dimensions(label)
            thumb = _thumbnail(label)
            if is_image and urlsplit(image_url).hostname == "avatars.mds.yandex.net":
                thumb = image_url
            result.hits.append(
                ImageHit(
                    engine="yandex",
                    image_url=image_url,
                    thumb_url=thumb,
                    page_url=None,
                    domain=domain(image_url),
                    first_seen=None,
                    time_source=None,
                    width=width,
                    height=height,
                    title=None,
                    rank=len(result.hits),
                    view="other_sizes" if title == "In other sizes" else "similar",
                )
            )
    if not found_section:
        raise ReverseImageError("Yandex returned no expected image sections")
    return result


def parse_yandex_sites(md: str, query_image_url: str = "") -> ReverseResult:
    """cbir_page=sites: pages containing the image (view "sites", with page_url and title)."""
    result = ReverseResult("yandex", query_image_url, yandex_url(query_image_url, sites=True))
    found_section = False
    for title, body in _sections(md):
        if title != "Sites containing information about the image":
            continue
        found_section = True
        for item in re.split(r"(?m)^[ \t]*[*+-][ \t]+", body)[1:]:
            links = iter(_markdown_links(item))
            label, url, _ = next(links, ("", "", False))
            image_url = _image_url(url)
            if not image_url:
                continue
            page_title, page_url = None, None
            for text, url, is_image in links:
                if not is_image and (page_url := _image_url(url)):
                    page_title = text.strip() or None
                    page_url = _strip_tracking(page_url)
                    break
            width, height = _dimensions(label)
            result.hits.append(
                ImageHit(
                    engine="yandex",
                    image_url=image_url,
                    thumb_url=_thumbnail(label),
                    page_url=page_url,
                    domain=domain(page_url or image_url),
                    first_seen=None,
                    time_source=None,
                    width=width,
                    height=height,
                    title=page_title,
                    rank=len(result.hits),
                    view="sites",
                )
            )
    if not found_section:
        raise ReverseImageError("Yandex returned no sites section")
    return result


def search_tineye(client: Any, image_url: str, *, deadline: float | None = None) -> ReverseResult:
    """client: sources.oxylabs.OxylabsClient. deadline: time.monotonic() bound."""
    markdown, _ = client.scrape_markdown(tineye_url(image_url), deadline=deadline)
    result = parse_tineye(markdown, image_url)  # sets request_url to the same tineye_url
    result.call = dict(client.last_call)
    return result


def search_yandex(
    client: Any, image_url: str, *, sites: bool = False, deadline: float | None = None
) -> ReverseResult:
    markdown, _ = client.scrape_markdown(yandex_url(image_url, sites=sites), deadline=deadline)
    result = (parse_yandex_sites if sites else parse_yandex)(markdown, image_url)
    result.call = dict(client.last_call)
    return result
