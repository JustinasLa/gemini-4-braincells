"""Read X posts through FxTwitter's public API (api.fxtwitter.com, the FxEmbed project).

FxTwitter serves X's own post data as JSON. It sees posts that X's logged-out page hides (posts
marked sensitive say "only available in the X app" there), gives exact counts (zeros included;
the logged-out page hides them) and the full text of long posts, and answers in under a second.
It is an unofficial community service, so every caller keeps the Oxylabs page read as the
fallback, and its receipts name it as the source.

`/2/conversation/<id>` returns the post and the first page of its replies (about 30), with a
cursor for more; `/2/status/<id>` the post alone. A missing post is HTTP 404 with
`{"status": null}`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from lookup.xpage import XItem

BASE_URL = "https://api.fxtwitter.com"
UA = "Mozilla/5.0 (compatible; shill-or-chill-lookup/1.0)"


class FxTwitterError(Exception):
    """FxTwitter didn't answer with the post (network error, rate limit, unknown format)."""


class FxNotFound(FxTwitterError):
    """FxTwitter says the post doesn't exist (deleted, or from a suspended/protected account).
    It doesn't say which: the Oxylabs page read can (lookup/xpage.unavailable_reason)."""


@dataclass
class Conversation:
    main: XItem
    parent_id: str | None = None  # the post it replies to
    parent_handle: str | None = None
    replies: list[XItem] = field(default_factory=list)
    cursor: str | None = None  # the next page of replies
    possibly_sensitive: bool = False


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _photos(st: dict) -> list[str]:
    media = st.get("media") or {}
    out = [p["url"] for p in media.get("photos") or [] if isinstance(p, dict) and p.get("url")]
    for v in media.get("videos") or []:  # a video's poster frame, like the page's video thumb
        if isinstance(v, dict) and isinstance(v.get("thumbnail_url"), str):
            out.append(v["thumbnail_url"])
    return [u for u in out if u.startswith("https://pbs.twimg.com/")]


def to_item(st: dict) -> XItem:
    """One FxTwitter status as the XItem the page parser would give (lookup/xpage.py)."""
    if not isinstance(st, dict) or not str(st.get("id") or "").isdigit():
        raise FxTwitterError("FxTwitter returned no post id")
    author = st.get("author") or {}
    handle = author.get("screen_name")
    if not isinstance(handle, str) or not handle:
        raise FxTwitterError("FxTwitter returned no author")
    q = st.get("quote") if isinstance(st.get("quote"), dict) else None
    q_author = (q or {}).get("author") or {}
    card = st.get("card") if isinstance(st.get("card"), dict) else {}
    return XItem(
        handle=handle,
        name=author.get("name") or None,
        status_id=str(st["id"]),
        text=st.get("text") or "",
        avatar_url=author.get("avatar_url") or None,
        is_main=True,
        quoted_handle=q_author.get("screen_name") if q else None,
        quoted_id=str(q["id"]) if q and str(q.get("id") or "").isdigit() else None,
        quoted_text=(q.get("text") or None) if q else None,
        replies=_int(st.get("replies")),
        reposts=_int(st.get("reposts")),
        likes=_int(st.get("likes")),
        bookmarks=_int(st.get("bookmarks")),
        views=_int(st.get("views")),
        media_urls=_photos(st),
        card_title=card.get("title") or None,
        card_url=card.get("url") or None,
        card_domain=card.get("domain") or None,
        card_image=None,
    )


class FxTwitterClient:
    """`last_call` mirrors sources.oxylabs.OxylabsClient's, for the receipts."""

    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        timeout_s: float = 8.0,
    ):
        self._own = http is None
        self._http = http or httpx.Client(headers={"User-Agent": UA}, follow_redirects=True)
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.last_call: dict = {}

    def close(self) -> None:
        if self._own:
            self._http.close()

    def _get(self, path: str, params: dict | None, deadline: float | None) -> dict:
        timeout = self.timeout_s
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0.2:
                raise FxTwitterError("no time left for FxTwitter")
        t0 = time.monotonic()
        url = f"{self.base_url}{path}"
        try:
            r = self._http.get(url, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FxTwitterError(f"FxTwitter request failed: {type(exc).__name__}") from exc
        finally:
            self.last_call = {
                "endpoint": path.rsplit("/", 1)[0],
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "fetched_at": _now(),
            }
        self.last_call["http_status"] = r.status_code
        try:
            data = r.json()
        except ValueError as exc:
            raise FxTwitterError(f"FxTwitter answered HTTP {r.status_code}, not JSON") from exc
        if not isinstance(data, dict):
            raise FxTwitterError(f"FxTwitter answered HTTP {r.status_code} with no object")
        if r.status_code == 404 or (r.status_code == 200 and data.get("status") is None):
            raise FxNotFound("FxTwitter: no such post")
        if r.status_code != 200:
            raise FxTwitterError(f"FxTwitter answered HTTP {r.status_code}")
        return data

    def status(self, status_id: str, *, deadline: float | None = None) -> Conversation:
        """The post alone."""
        data = self._get(f"/2/status/{status_id}", None, deadline)
        return self._conversation(data, with_replies=False)

    def conversation(
        self, status_id: str, *, cursor: str | None = None, deadline: float | None = None
    ) -> Conversation:
        """The post and one page of its replies (the next page with `cursor`)."""
        params = {"cursor": cursor} if cursor else None
        data = self._get(f"/2/conversation/{status_id}", params, deadline)
        return self._conversation(data, with_replies=True)

    @staticmethod
    def _conversation(data: dict, *, with_replies: bool) -> Conversation:
        st = data.get("status")
        main = to_item(st)
        parent = st.get("replying_to") if isinstance(st.get("replying_to"), dict) else {}
        pid = str(parent.get("status") or "")
        replies: list[XItem] = []
        if with_replies:
            for r in data.get("replies") or []:
                try:
                    it = to_item(r)
                except FxTwitterError:
                    continue
                it.is_main = False
                replies.append(it)
        cursor = (data.get("cursor") or {}).get("bottom") if with_replies else None
        return Conversation(
            main=main,
            parent_id=pid if pid.isdigit() else None,
            parent_handle=parent.get("screen_name") or None,
            replies=replies,
            cursor=cursor if isinstance(cursor, str) and cursor else None,
            possibly_sensitive=bool(st.get("possibly_sensitive")),
        )


def _now() -> datetime:
    return datetime.now(UTC)
