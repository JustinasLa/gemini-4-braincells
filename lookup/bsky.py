"""Bluesky public AppView (read-only, no key): posts, profiles and post search."""

from __future__ import annotations

import logging
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
        self.last_call: dict[str, Any] = {}

    def close(self) -> None:
        self._http.close()

    def _get(self, method: str, params: dict) -> dict:
        started = time.monotonic()
        res = self._http.get(f"/{method}", params=params)
        self.last_call = {
            "endpoint": method,
            "status": res.status_code,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        log.info(
            "bluesky call endpoint=%s status=%s latency_ms=%d",
            method,
            res.status_code,
            self.last_call["latency_ms"],
        )
        res.raise_for_status()
        return res.json()

    def resolve_did(self, actor: str) -> str:
        if actor.startswith("did:"):
            return actor
        return self._get("com.atproto.identity.resolveHandle", {"handle": actor})["did"]

    def thread(self, actor: str, rkey: str, depth: int = 1) -> dict:
        did = self.resolve_did(actor)
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        return self._get(
            "app.bsky.feed.getPostThread", {"uri": uri, "depth": depth, "parentHeight": 3}
        )["thread"]

    def profile(self, actor: str) -> dict:
        return self._get("app.bsky.actor.getProfile", {"actor": actor})

    def author_feed(self, actor: str, limit: int = 20) -> list[dict]:
        data = self._get(
            "app.bsky.feed.getAuthorFeed",
            {"actor": actor, "limit": limit, "filter": "posts_no_replies"},
        )
        return [f["post"] for f in data.get("feed", []) if "post" in f]

    def search(self, q: str, limit: int = 25) -> list[dict]:
        return self._get(
            "app.bsky.feed.searchPosts", {"q": q, "limit": limit, "sort": "latest"}
        ).get("posts", [])
