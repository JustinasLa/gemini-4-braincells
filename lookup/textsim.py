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


def n_words(text: str | None) -> int:
    """Words in `text`, stop words included (what caption_runs counts in a run)."""
    return len(_TOKEN.findall(text or ""))


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


# A site address written in an image ("standwithjensen.com"): the most distinctive thing a
# screenshot of a web page carries. Big platforms' own names say nothing about the content.
_DOMAIN = re.compile(
    r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.I
)
_NOT_TLD = frozenset(
    "png jpg jpeg gif webp heic svg pdf txt doc docx xls xlsx ppt pptx csv json xml html htm "
    "mp4 mov avi mp3 wav zip rar exe dmg apk js ts py md".split()
)
_SECOND_LEVEL = frozenset("co com org net gov ac edu ne or go".split())  # bbc.co.uk -> bbc
_PLATFORMS = frozenset(
    "x.com twitter.com t.co bsky.app bsky.social facebook.com fb.com instagram.com "
    "threads.net threads.com tiktok.com youtube.com youtu.be reddit.com linkedin.com "
    "google.com bing.com yandex.com telegram.org t.me whatsapp.com discord.com discord.gg "
    "medium.com substack.com wikipedia.org imgur.com pinterest.com tumblr.com".split()
)
DOMAIN_LABEL_MIN = 8  # a bare name part ("#standwithjensen") matches only from this length


def domain_label(domain: str) -> str:
    """The name part of a site address: standwithjensen.com -> standwithjensen."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else domain.lower()


def domains(text: str | None, limit: int = 2) -> list[str]:
    """Site addresses written in `text`, in order, without big platforms' own (x.com, ...),
    file names (photo.jpg) or short labels (e.g, a.m)."""
    out: list[str] = []
    for m in _DOMAIN.finditer(text or ""):
        d = m.group(1).lower().removeprefix("www.")
        if d.rsplit(".", 1)[-1] in _NOT_TLD or len(domain_label(d)) < 4:
            continue
        if any(d == p or d.endswith("." + p) for p in _PLATFORMS):
            continue
        if d not in out:
            out.append(d)
    return out[:limit]


def domain_hits(text: str | None, doms: list[str]) -> list[str]:
    """Where `text` names one of the sites `doms`, as written (for highlighting): the address
    itself, or its name part (8+ letters) as a hashtag (#standwithjensen) or under another
    ending (standwithjensen.online). Never the name part as a plain word: information.com's
    "information" is in ordinary sentences."""
    if not text or not doms:
        return []
    out: list[str] = []
    for d in doms:
        label = domain_label(d)
        name = re.escape(label)
        # another domain ending (standwithjensen.online), never a file name (analytics.js)
        not_file = "|".join(sorted(_NOT_TLD))
        other_ending = rf"{name}(?=\.(?!(?:{not_file})(?![\w-]))[a-z]{{2,24}}(?![\w-]))"
        alts = re.escape(d) + (f"|#{name}|{other_ending}" if len(label) >= DOMAIN_LABEL_MIN else "")
        # the full address first, so "StandWithJensen.com" is one match, not two
        for m in re.finditer(rf"(?<![\w#-])(?:{alts})(?![\w-])", text, re.I):
            if m.group() not in out:
                out.append(m.group())
    return out
