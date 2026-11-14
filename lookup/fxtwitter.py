"""Read X posts through FxTwitter's public API (api.fxtwitter.com, the FxEmbed project).

FxTwitter serves X's own post data as JSON. It sees posts that X's logged-out page hides (posts
marked sensitive say "only available in the X app" there), gives exact counts (zeros included;
the logged-out page hides them) and the full text of long posts, and answers in under a second.
It is an unofficial community service, so every caller keeps the Oxylabs page read as the
fallback, and its receipts name it as the source.

`/2/conversation/<id>` returns the post and the first page of its replies (about 30), with a
cursor for more; `/2/status/<id>` the post alone. A missing post is HTTP 404 with
`{"status": null}` (deleted) or a `"tombstone"` status (suspended account).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from lookup.xpage import XItem

BASE_URL = "https://api.fxtwitter.com"
UA = "Mozilla/5.0 (compatible; shill-or-chill-lookup/1.0)"
OP_TIMEOUT_S = 2.0  # longest single network wait, so a worker left behind ends soon too
MAX_BYTES = 4 * 1024 * 1024  # a conversation page is ~100 KB


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


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _pbs(v: Any) -> str | None:
    """An X image URL (pbs.twimg.com), else None: nothing else is downloaded from a reply."""
    return v if isinstance(v, str) and v.startswith("https://pbs.twimg.com/") else None


def _photos(st: dict) -> list[str]:
    media = _dict(st.get("media"))
    photos = media.get("photos") if isinstance(media.get("photos"), list) else []
    videos = media.get("videos") if isinstance(media.get("videos"), list) else []
    out = [u for p in photos if (u := _pbs(_dict(p).get("url")))]
    # a video's poster frame, like the page's video thumb
    out += [u for v in videos if (u := _pbs(_dict(v).get("thumbnail_url")))]
    return out


def _id(v: Any) -> str | None:
    s = str(v) if isinstance(v, int | str) and not isinstance(v, bool) else ""
    return s if s.isdigit() else None


def to_item(st: Any) -> XItem:
    """One FxTwitter status as the XItem the page parser would give (lookup/xpage.py).
    Raises FxTwitterError when it isn't a readable post (malformed or missing fields)."""
    st = _dict(st)
    sid = _id(st.get("id"))
    if not sid:
        raise FxTwitterError("FxTwitter returned no post id")
    author = _dict(st.get("author"))
    handle = _str(author.get("screen_name"))
    if not handle:
        raise FxTwitterError("FxTwitter returned no author")
    text = st.get("text")
    if not isinstance(text, str):  # "" is a real (media-only) post; a missing text is not
        raise FxTwitterError("FxTwitter returned no text")
    q = _dict(st.get("quote"))
    q_id = _id(q.get("id")) if q else None
    # like the page parser (lookup/xpage.py), a quoted post's card and pictures are read with
    # the post that quotes it: they show on its page, and the linked page is traced from them
    card = _dict(st.get("card")) or (_dict(q.get("card")) if q_id else {})
    media = _photos(st)
    media += [u for u in (_photos(q) if q_id else []) if u not in media]
    return XItem(
        handle=handle,
        name=_str(author.get("name")),
        status_id=sid,
        text=text,
        avatar_url=_str(author.get("avatar_url")),
        is_main=True,
        quoted_handle=_str(_dict(q.get("author")).get("screen_name")) if q_id else None,
        quoted_id=q_id,
        quoted_text=_str(q.get("text")) if q_id else None,
        replies=_int(st.get("replies")),
        reposts=_int(st.get("reposts")),
        likes=_int(st.get("likes")),
        bookmarks=_int(st.get("bookmarks")),
        views=_int(st.get("views")),
        media_urls=media,
        card_title=_str(card.get("title")),
        card_url=_str(card.get("url")),
        card_domain=_str(card.get("domain")),
        # the link card's thumbnail: read like the page's card image when there's no photo
        card_image=_pbs(_dict(card.get("image")).get("url")),
    )


class FxTwitterClient:
    """`last_call` mirrors sources.oxylabs.OxylabsClient's, for the receipts.

    Every read has its own connection and runs in a worker thread: at its bound the caller
    stops waiting and closes that connection, so no part of the exchange (connecting, the
    headers, a redirect, the body) can hold the lookup past it, however slowly a server
    trickles bytes. httpx's own timeout is per network operation, not per request."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout_s: float = 8.0,
        transport: httpx.MockTransport | None = None,
    ):
        # a real transport could hand a read a pooled keep-alive connection that opens no
        # socket, which a cancel could then not reach: every real read gets a fresh one
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise TypeError("FxTwitterClient takes only an httpx.MockTransport (tests)")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport
        self._local = threading.local()

    @property
    def last_call(self) -> dict:
        """{endpoint, http_status, latency_ms, fetched_at} of this thread's latest call: the
        lookup's workers share one client, so each reads its own call's metadata."""
        return getattr(self._local, "last_call", {})

    def close(self) -> None:
        """Nothing to close: each read opens and closes its own connection."""

    def _get(self, path: str, params: dict | None, deadline: float | None) -> dict:
        """One GET that ends by `deadline` (and timeout_s) as a whole."""
        end = time.monotonic() + self.timeout_s
        if deadline is not None:
            end = min(end, deadline)
        t0 = time.monotonic()
        if end - t0 <= 0.2:
            raise FxTwitterError("no time left for FxTwitter")
        read = _Read(self, path, params, min(OP_TIMEOUT_S, end - t0))
        worker = threading.Thread(target=read.run, name="fxtwitter-read", daemon=True)
        worker.start()
        worker.join(max(0.0, end - time.monotonic()))
        late = worker.is_alive()
        if late:
            read.cancel()  # its socket is shut down now or the moment it connects
        status = read.status
        self._local.last_call = {
            "endpoint": path.rsplit("/", 1)[0],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "fetched_at": _now(),
            "http_status": status,
        }
        if late:
            raise FxTwitterError("FxTwitter answer not complete in time")
        if isinstance(read.error, FxTwitterError):
            raise read.error
        if read.error is not None:
            raise FxTwitterError(f"FxTwitter request failed: {type(read.error).__name__}")
        try:
            data = json.loads(read.body)
        except ValueError as exc:
            raise FxTwitterError(f"FxTwitter answered HTTP {status}, not JSON") from exc
        if not isinstance(data, dict):
            raise FxTwitterError(f"FxTwitter answered HTTP {status} with no object")
        # a missing post is HTTP 404: deleted {"status": null, "code": 404}; suspended (and the
        # like) {"status": {"type": "tombstone", "reason": "suspended", ...}}. Any other answer
        # without a post (an odd 404 body, an empty object, an upstream error) is not "gone":
        # the caller reads the X page, which says why
        st = data.get("status")
        tomb = isinstance(st, dict) and st.get("type") == "tombstone"
        if (
            status == 404
            and "status" in data
            and (st is None or tomb)
            and data.get("code", 404) == 404
        ):
            reason = _str(st.get("reason")) if tomb else None
            raise FxNotFound(f"FxTwitter: no such post ({reason or 'gone'})")
        if status != 200:
            raise FxTwitterError(f"FxTwitter answered HTTP {status}")
        if not isinstance(st, dict) or tomb:
            raise FxTwitterError("FxTwitter answered with no post")
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
        """The answer as a Conversation. A malformed answer is an FxTwitterError (the caller
        falls back to the page read); a malformed reply is skipped, not the whole post."""
        try:
            st = _dict(data.get("status"))
            main = to_item(st)
            parent = _dict(st.get("replying_to"))
            pid = _id(parent.get("status"))
            replies: list[XItem] = []
            raw = data.get("replies") if with_replies else None
            for r in raw if isinstance(raw, list) else []:
                try:
                    it = to_item(r)
                except FxTwitterError:
                    continue
                it.is_main = False
                replies.append(it)
            cursor = _dict(data.get("cursor")).get("bottom") if with_replies else None
            return Conversation(
                main=main,
                parent_id=pid,
                parent_handle=_str(parent.get("screen_name")) if pid else None,
                replies=replies,
                cursor=_str(cursor),
                possibly_sensitive=st.get("possibly_sensitive") is True,
            )
        except FxTwitterError:
            raise
        except Exception as exc:  # any other shape surprise: not a readable answer
            raise FxTwitterError(f"FxTwitter answer not understood: {type(exc).__name__}") from exc


class _Read:
    """One FxTwitter GET on its own connection, run by a worker thread, that the caller can
    cancel at any point: connecting (httpcore's trace hook hands over each socket as it
    opens; one opened after the cancel is shut at once), the headers or the body (the socket
    is shut down, which ends a blocked read). The worker closes its own client however it
    ends, so a cancelled read leaves no thread or socket behind."""

    def __init__(self, client: FxTwitterClient, path: str, params: dict | None, op_s: float):
        self.url = f"{client.base_url}{path}"
        self.params = params
        self.http = httpx.Client(
            headers={"User-Agent": UA},
            follow_redirects=True,
            transport=client._transport,
            timeout=op_s,
        )
        self.lock = threading.Lock()
        self.cancelled = False
        self.streams: list[Any] = []
        self.status: int | None = None
        self.body = b""
        self.error: BaseException | None = None

    @staticmethod
    def _shut(stream: Any) -> None:
        sock = stream.get_extra_info("socket") if hasattr(stream, "get_extra_info") else None
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _trace(self, name: str, info: dict) -> None:
        # every socket the read opens or wraps in TLS: directly ("connection."), through an
        # HTTP CONNECT proxy ("proxy.start_tls": TLS wrapping detaches the TCP socket recorded
        # before, so the TLS one must be recorded too) or a SOCKS proxy ("socks.")
        if name.endswith((".connect_tcp.complete", ".start_tls.complete")):
            stream = info.get("return_value")
            with self.lock:
                if stream is not None:
                    self.streams.append(stream)
                late = self.cancelled
            if late and stream is not None:
                self._shut(stream)
        with self.lock:
            if self.cancelled:
                raise FxTwitterError("FxTwitter read cancelled")

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            streams = list(self.streams)
        for stream in streams:
            self._shut(stream)

    def run(self) -> None:
        try:
            with self.http.stream(
                "GET", self.url, params=self.params, extensions={"trace": self._trace}
            ) as r:
                self.status = r.status_code
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if self.cancelled:
                        raise FxTwitterError("FxTwitter read cancelled")
                    if len(body) > MAX_BYTES:
                        raise FxTwitterError("FxTwitter answer too large")
                self.body = bytes(body)
        except BaseException as exc:  # handed to the caller
            self.error = exc
        finally:
            self.http.close()


def _now() -> datetime:
    return datetime.now(UTC)
