"""Bluesky public AppView (read-only, no key): posts, profiles and post search."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

APPVIEW = "https://api.bsky.app/xrpc"  # public.api.bsky.app refuses searchPosts (403)


def post_url(uri: str, handle: str | None) -> str:
    """at://did/app.bsky.feed.post/rkey -> https://bsky.app/profile/<handle or did>/post/rkey"""
    parts = uri.removeprefix("at://").split("/")
    return f"https://bsky.app/profile/{handle or parts[0]}/post/{parts[-1]}"


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class BlueskyTimeUp(RuntimeError):
    """The caller's deadline passed before a request could start."""


class BlueskyClient:
    def __init__(
        self,
        *,
        base_url: str = APPVIEW,
        timeout_s: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"user-agent": "shill-or-chill-lookup/1.0"},
        )
        self._local = threading.local()  # last_call per thread: parallel calls don't race
        self._timeout_s = timeout_s

    @property
    def last_call(self) -> dict[str, Any]:
        """{endpoint, status, latency_ms} of this thread's latest call (receipts). `status` is
        None when the request itself failed (timeout, connection error)."""
        return getattr(self._local, "last_call", {})

    def close(self) -> None:
        self._http.close()

    def _get(self, method: str, params: dict, deadline: float | None = None) -> dict:
        """One AppView call; `deadline` (time.monotonic()) clamps each attempt and stops the
        retry once it has passed."""

        def attempt() -> httpx.Response:
            timeout = None
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 1:
                    raise BlueskyTimeUp(f"bluesky {method}: time budget used up")
                timeout = min(self._timeout_s, left)
            return self._http.get(f"/{method}", params=params, timeout=timeout or self._timeout_s)

        started = time.monotonic()
        self._local.last_call = {}
        try:
            res = attempt()
            if res.status_code in (502, 503, 504):  # the public AppView blips now and then: again
                time.sleep(0.5)
                try:
                    res = attempt()
                except BlueskyTimeUp:  # no time for the retry: the first answer is the fact
                    pass
        except httpx.HTTPError:
            self._local.last_call = {
                "endpoint": method,
                "status": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
            raise
        self._local.last_call = call = {
            "endpoint": method,
            "status": res.status_code,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        log.info(
            "bluesky call endpoint=%s status=%s latency_ms=%d",
            method,
            res.status_code,
            call["latency_ms"],
        )
        res.raise_for_status()
        return res.json()

    def resolve_did(self, actor: str, deadline: float | None = None) -> str:
        if actor.startswith("did:"):
            return actor
        return self._get("com.atproto.identity.resolveHandle", {"handle": actor}, deadline)["did"]

    def thread(self, actor: str, rkey: str, depth: int = 1, deadline: float | None = None) -> dict:
        did = self.resolve_did(actor, deadline)
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        return self._get(
            "app.bsky.feed.getPostThread", {"uri": uri, "depth": depth, "parentHeight": 3}, deadline
        )["thread"]

    def profile(self, actor: str, deadline: float | None = None) -> dict:
        return self._get("app.bsky.actor.getProfile", {"actor": actor}, deadline)

    def author_feed(self, actor: str, limit: int = 20, deadline: float | None = None) -> list[dict]:
        return [f["post"] for f in self.author_feed_items(actor, limit, deadline) if "post" in f]

    def author_feed_items(
        self,
        actor: str,
        limit: int = 20,
        deadline: float | None = None,
        filter: str = "posts_no_replies",
    ) -> list[dict]:
        """The raw feed items ({post, reason?, reply?}): `reason` marks a repost or a pin."""
        data = self._get(
            "app.bsky.feed.getAuthorFeed",
            {"actor": actor, "limit": limit, "filter": filter},
            deadline,
        )
        return list(data.get("feed", []))

    def search(self, q: str, limit: int = 25, deadline: float | None = None) -> list[dict]:
        return self._get(
            "app.bsky.feed.searchPosts", {"q": q, "limit": limit, "sort": "latest"}, deadline
        ).get("posts", [])
