"""Pure helpers: which links a cluster shares, and which phrase to search the web for."""

import re
from collections import Counter
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sources.models import ClusterPost

# Links that point back into Bluesky itself say nothing about outside sources.
SKIP_HOSTS = frozenset({"bsky.app", "go.bsky.app"})
MEDIA_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".m3u8")
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "si"}
)

_URL = re.compile(r"https?://\S+")
_MENTION_OR_TAG = re.compile(r"(?<!\w)[@#][\w.\-]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def normalize_url(url: str) -> str | None:
    """Canonical form for counting and caching. None for links we don't check."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    if host in SKIP_HOSTS or host == "localhost" or re.fullmatch(r"[\d.:]+", host):
        return None
    if parts.path.lower().endswith(MEDIA_SUFFIXES):
        return None
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
        ]
    )
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def top_links(posts: Iterable[ClusterPost], k: int) -> list[tuple[str, int]]:
    """The k links shared by the most distinct accounts: [(normalized_url, n_accounts)]."""
    sharers: dict[str, set[str]] = {}
    for p in posts:
        for raw in p.embed_urls:
            url = normalize_url(raw)
            if url:
                sharers.setdefault(url, set()).add(p.did)
    ranked = sorted(sharers.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(url, len(dids)) for url, dids in ranked[:k]]


def _clean(text: str) -> str:
    text = _MENTION_OR_TAG.sub(" ", _URL.sub(" ", text))
    return re.sub(r"[ \t]+", " ", text).strip()


def claim_phrase(
    texts: Iterable[str | None], *, min_words: int = 5, max_words: int = 12
) -> str | None:
    """A distinctive phrase from the cluster's most repeated text, for an exact-phrase search.

    Takes the longest sentence of the most common (cleaned) text and cuts it to `max_words`.
    None when no sentence has at least `min_words` words: short phrases match too much of the web.
    """
    cleaned = [c for c in (_clean(t) for t in texts if t) if c]
    if not cleaned:
        return None
    common, _ = Counter(c.lower() for c in cleaned).most_common(1)[0]
    original = next(c for c in cleaned if c.lower() == common)
    sentences = [s.strip(" \"'“”") for s in _SENTENCE_SPLIT.split(original)]
    best = max(sentences, key=lambda s: len(s.split()), default="")
    words = best.split()
    if len(words) < min_words:
        return None
    return " ".join(words[:max_words]).rstrip(",;:")
