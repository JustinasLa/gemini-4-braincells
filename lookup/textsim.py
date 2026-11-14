"""Token similarity between a post's text and a candidate copy (search snippet or page text)."""

from __future__ import annotations

import re

_URL = re.compile(r"https?://\S+")
_TOKEN = re.compile(r"[\w']+", re.UNICODE)
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": " ", "–": " "})
_LEADING_DATE = re.compile(
    r"^\s*(?:\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago|"
    r"[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})\s*[·—–\-]\s*"
)
STOP = frozenset("a an the and or of to in on at is it that this for with as by be are was".split())


def tokens(text: str | None) -> list[str]:
    if not text:
        return []
    t = _URL.sub(" ", text.translate(_QUOTES).lower())
    return [w for w in _TOKEN.findall(t) if w not in STOP]


def strip_snippet(snippet: str) -> str:
    """Drop the '23 hours ago · ' / 'Nov 13, 2026 — ' prefix and a trailing ellipsis."""
    s = _LEADING_DATE.sub("", snippet or "").strip()
    return re.sub(r"\s*(?:\.\.\.|…)\s*$", "", s)


def similarity(candidate: str | None, reference: str | None, *, truncated: bool = False) -> float:
    """0..1: how much of the reference text the candidate carries.

    `truncated` (a search snippet): share of the candidate's words found in the reference.
    Otherwise the larger of the Jaccard overlap and the share of the reference's words the
    candidate contains (so "Stephen King: <the same text>" still counts as carrying it), the
    latter only when the reference has 6+ distinct words (short texts match too easily)."""
    a, b = set(tokens(candidate)), set(tokens(reference))
    if not a or not b:
        return 0.0
    if truncated:
        return len(a & b) / len(a)
    jac = len(a & b) / len(a | b)
    if len(b) < 6:
        return jac
    return max(jac, 0.95 * len(a & b) / len(b))


def phrase(text: str | None, n_words: int = 12) -> str | None:
    """An exact-search phrase: the first `n_words` words of the text, without URLs or mentions.
    None when the text is too short (< 5 words) to search for reliably."""
    if not text:
        return None
    t = _URL.sub(" ", text.translate(_QUOTES))
    words = [w for w in t.split() if not w.startswith("@") and not w.startswith("#")]
    if len(words) < 5:
        return None
    out = " ".join(words[:n_words])
    return out.replace('"', "").strip(" .,:;")
