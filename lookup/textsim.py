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


def phrase(text: str | None, n_words: int = 12, *, min_words: int = 5) -> str | None:
    """An exact-search phrase: the first `n_words` words of the text, without URLs or mentions.
    None when the text is too short (< `min_words` words) to search for reliably."""
    if not text:
        return None
    t = _URL.sub(" ", text.translate(_QUOTES))
    words = [w for w in t.split() if not w.startswith("@") and not w.startswith("#")]
    if len(words) < min_words:
        return None
    out = " ".join(words[:n_words])
    return out.replace('"', "").strip(" .,:;")


RUN_MIN = 3  # words in a row of an uploaded image's caption that make a text hit a match


def caption_runs(text: str | None, caption: str | None, min_words: int = RUN_MIN) -> list[str]:
    """The parts of `text` that repeat at least `min_words` words in a row of `caption`, as they
    are written in `text` (for highlighting), longest-first per position and never overlapping.

    Words compare case-insensitively, ignoring punctuation and curly quotes. A run made only of
    stop words ("is in the") doesn't count."""
    if not text or not caption:
        return []
    cap = [w.lower() for w in _TOKEN.findall(caption.translate(_QUOTES))]
    if len(cap) < min_words:
        return []
    # translate() maps one character to one, so the spans index the original text
    toks = list(_TOKEN.finditer(text.translate(_QUOTES)))
    words = [m.group().lower() for m in toks]
    starts: dict[str, list[int]] = {}
    for j, w in enumerate(cap):
        starts.setdefault(w, []).append(j)
    out, i = [], 0
    while i < len(words):
        best = 0
        for j in starts.get(words[i], ()):
            k = 0
            while i + k < len(words) and j + k < len(cap) and words[i + k] == cap[j + k]:
                k += 1
            best = max(best, k)
        if best >= min_words and any(w not in STOP for w in words[i : i + best]):
            out.append(text[toks[i].start() : toks[i + best - 1].end()])
            i += best
        else:
            i += 1
    return out
