"""Classify a submitted link, and read the creation time hidden in X post ids."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

# X ("Twitter") snowflake ids: milliseconds since this epoch, shifted left by 22 bits.
TWITTER_EPOCH_MS = 1288834974657
# Ids before snowflakes (2010-11) carry no time; anything this small is not a snowflake.
MIN_SNOWFLAKE = 1 << 32

X_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "fixupx.com",
    "nitter.net",
}
BSKY_HOSTS = {"bsky.app", "www.bsky.app", "staging.bsky.app"}
X_RESERVED = {"i", "home", "explore", "search", "settings", "notifications", "messages", "intent"}

X_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,20})"
)


@dataclass(frozen=True)
class Target:
    kind: str  # x_post | x_profile | bsky_post | bsky_profile | web
    url: str  # canonical URL
    handle: str | None = None
    post_id: str | None = None  # X status id or Bluesky rkey


def snowflake_time(status_id: str | int) -> datetime | None:
    """Creation time encoded in an X post id, or None for invalid/pre-snowflake ids."""
    try:
        n = int(status_id)
        if n < MIN_SNOWFLAKE:
            return None
        return datetime.fromtimestamp(((n >> 22) + TWITTER_EPOCH_MS) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def x_post_url(handle: str, status_id: str) -> str:
    return f"https://x.com/{handle}/status/{status_id}"


def classify(raw: str) -> Target | None:
    """None when `raw` is not a parseable http(s) URL."""
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        return None
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:  # malformed brackets or an invalid Unicode authority
        return None
    if not host or "." not in host:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if host in X_HOSTS:
        if len(segs) >= 4 and segs[0] == "i" and segs[1] == "web" and segs[3].isdigit():
            return Target("x_post", f"https://x.com/i/web/status/{segs[3]}", None, segs[3])
        if len(segs) >= 3 and segs[1] in ("status", "statuses") and segs[2].isdigit():
            if segs[0].lower() == "i":  # x.com/i/status/<id>: no handle in the link
                return Target("x_post", f"https://x.com/i/web/status/{segs[2]}", None, segs[2])
            return Target("x_post", x_post_url(segs[0], segs[2]), segs[0], segs[2])
        if len(segs) == 1 and segs[0].lower() not in X_RESERVED:
            return Target("x_profile", f"https://x.com/{segs[0]}", segs[0])
    if host in BSKY_HOSTS and len(segs) >= 2 and segs[0] == "profile":
        if len(segs) >= 4 and segs[2] == "post":
            return Target(
                "bsky_post", f"https://bsky.app/profile/{segs[1]}/post/{segs[3]}", segs[1], segs[3]
            )
        return Target("bsky_profile", f"https://bsky.app/profile/{segs[1]}", segs[1])
    clean = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    return Target("web", clean)


def domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return host.removeprefix("www.") or None
