"""Pure helpers: which links a cluster shares, and which phrase to search the web for."""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from urllib.parse import urlsplit

from signals.textnorm import UNSPACED_RE, normalize_url
from sources.models import ClusterPost

# Links that point back into Bluesky itself say nothing about outside sources.
SKIP_HOSTS = frozenset({"bsky.app", "go.bsky.app"})
MEDIA_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".m3u8")

_URL = re.compile(r"https?://\S+")
_MENTION_OR_TAG = re.compile(r"(?<!\w)[@#][\w.\-]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*|\n+")


def checkable_url(url: str) -> str | None:
    """The canonical URL (same form ingest stores) if it is worth a source check, else None."""
    norm = normalize_url(url)
    if not norm:
        return None
    parts = urlsplit(norm)
    host = parts.hostname or ""
    if host in SKIP_HOSTS or host == "localhost" or re.fullmatch(r"[\d.:]+", host):
        return None
    if parts.path.lower().endswith(MEDIA_SUFFIXES):
        return None
    return norm


def domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def top_links(posts: Iterable[ClusterPost], k: int) -> list[tuple[str, int]]:
    """The k links shared by the most distinct accounts: [(normalized_url, n_accounts)]."""
    sharers: dict[str, set[str]] = {}
    for p in posts:
        for raw in p.embed_urls:
            url = checkable_url(raw)
            if url:
                sharers.setdefault(url, set()).add(p.did)
    ranked = sorted(sharers.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [(url, len(dids)) for url, dids in ranked[:k]]


def _clean(text: str) -> str:
    text = _MENTION_OR_TAG.sub(" ", _URL.sub(" ", text))
    return re.sub(r"[ \t]+", " ", text).strip()


def _unspaced_chars(s: str) -> int:
    """Characters of unspaced scripts in `s`, not counting combining marks (Thai vowel signs)."""
    return sum(1 for c in UNSPACED_RE.findall(s) if not unicodedata.category(c).startswith("M"))


def _size(s: str) -> float:
    """Sentence length in words; unspaced scripts count two characters as one word."""
    n = _unspaced_chars(s)
    return n / 2 if n else len(s.split())


def _cut_chars(s: str, max_chars: int) -> str:
    """Whole space-separated chunks up to `max_chars`, else the first chunk cut there
    (never between a character and its combining marks, as in Thai)."""
    out = ""
    for chunk in s.split():
        joined = f"{out} {chunk}" if out else chunk
        if len(joined) > max_chars:
            break
        out = joined
    if out:
        return out
    end = max_chars
    while end < len(s) and unicodedata.category(s[end]).startswith("M"):
        end += 1
    return s[:end]


def claim_phrase(
    texts: Iterable[str | None],
    *,
    min_words: int = 5,
    max_words: int = 12,
    min_chars: int = 10,
    max_chars: int = 30,
) -> str | None:
    """A distinctive phrase from the cluster's most repeated text, for an exact-phrase search.

    Takes the longest sentence of the most common (cleaned) text and cuts it to `max_words`.
    None when no sentence has at least `min_words` words: short phrases match too much of the web.
    Sentences in unspaced scripts (Japanese, Chinese, Thai) are measured in characters instead:
    at least `min_chars` of them, cut to `max_chars`.
    """
    cleaned = [c for c in (_clean(t) for t in texts if t) if c]
    if not cleaned:
        return None
    common, _ = Counter(c.lower() for c in cleaned).most_common(1)[0]
    original = next(c for c in cleaned if c.lower() == common)
    sentences = [s.strip(" \"'“”「」『』") for s in _SENTENCE_SPLIT.split(original)]
    best = max(sentences, key=_size, default="")
    if UNSPACED_RE.search(best):
        if _unspaced_chars(best) < min_chars:
            return None
        return _cut_chars(best, max_chars).rstrip(",;:、，")
    words = best.split()
    if len(words) < min_words:
        return None
    return " ".join(words[:max_words]).rstrip(",;:")
