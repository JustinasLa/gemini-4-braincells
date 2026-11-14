"""Text and URL normalisation shared by ingest (text_hash, embed_urls) and signals (similarity).

Kept dependency-free and deterministic: the same input always gives the same output.
"""

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/\S*)?", re.IGNORECASE)
_MENTION_RE = re.compile(r"@[\w.-]+")


def _mark_class() -> str:
    """Regex class body for combining marks (Unicode category M) in the BMP: Thai and Lao vowels
    and tone marks, Indic vowel signs, Arabic and Hebrew points... `\\w` doesn't include them, so
    without this, words in those scripts were cut into pieces."""
    ranges, start, prev = [], None, None
    for cp in range(0x10000):
        is_mark = unicodedata.category(chr(cp)).startswith("M")
        if is_mark and start is None:
            start = cp
        if not is_mark and start is not None:
            ranges.append((start, prev))
            start = None
        prev = cp
    return "".join(f"\\u{a:04x}-\\u{b:04x}" if a != b else f"\\u{a:04x}" for a, b in ranges)


_NON_WORD_RE = re.compile(r"[^\w#" + _mark_class() + r"]+", re.UNICODE)
# Scripts written without spaces between words: Han, kana, Thai, Lao, Khmer, Myanmar.
_UNSPACED_RE = re.compile(
    "[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]"
)
CHARS_PER_WORD = 2  # rough word length in unspaced scripts (CJK ~1.5-2, Thai longer)
_WS_RE = re.compile(r"\s+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)

# Query parameters that only track clicks; dropping them makes the same link compare equal.
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "ref",
    "ref_src",
    "ref_url",
    "si",
    "feature",
    "share",
    "s",
    "t",
    "smid",
    "cmpid",
    "yclid",
    "twclid",
    "ttclid",
    "spm",
    "src",
    "via",
}


def normalize_text(text: str | None) -> str:
    """Lowercase, NFKC, strip URLs, @mentions, emoji and punctuation; keep words and #tags."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _URL_RE.sub(" ", t)
    t = _BARE_DOMAIN_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = _NON_WORD_RE.sub(" ", t)  # emoji, punctuation, symbols
    t = t.replace("_", " ")
    return _WS_RE.sub(" ", t).strip()


def tokens(norm_text: str) -> list[str]:
    return norm_text.split() if norm_text else []


def _unspaced(token: str) -> int:
    return len(_UNSPACED_RE.findall(token))


def is_unspaced(norm_text: str) -> bool:
    """Mostly written in a script without spaces between words."""
    letters = norm_text.replace(" ", "")
    return bool(letters) and _unspaced(letters) * 2 >= len(letters)


def word_count(norm_text: str) -> int:
    """Words, counting a run of unspaced-script characters as about one word per two characters
    (a long Japanese sentence is one whitespace token but ~20 words)."""
    n = 0
    for tok in tokens(norm_text):
        u = _unspaced(tok)
        n += max(1, -(-u // CHARS_PER_WORD)) if u * 2 >= len(tok) else 1
    return n


def shingles(norm_text: str) -> set[str]:
    """Near-duplicate shingles: word 3-grams, or character 3-grams for unspaced scripts."""
    if is_unspaced(norm_text):
        s = norm_text.replace(" ", "")
        return {s[i : i + 3] for i in range(max(1, len(s) - 2))}
    toks = tokens(norm_text)
    return {" ".join(toks[i : i + 3]) for i in range(max(1, len(toks) - 2))}


def text_hash(text: str | None) -> str | None:
    """Stable 16-hex hash of the normalised text, or None when nothing is left after normalising."""
    norm = normalize_text(text)
    if not norm:
        return None
    return hashlib.blake2b(norm.encode(), digest_size=8).hexdigest()


MAX_URL_LEN = 1000  # Postgres index entries max out near 2.7 kB; real links are far shorter


def normalize_url(url: str | None) -> str | None:
    """Canonical form for comparing links: https, lowercase host without www., no fragment,
    no tracking params, sorted query, no trailing slash. Resolves nothing (no network)."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        if _SCHEME_RE.match(url):  # mailto:, javascript:, data: ...
            return None
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        return None
    # hostname omits IPv6 brackets; the URL authority needs them, including before a port.
    if ":" in host:
        host = f"[{host}]"
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path or ""
    if path.endswith("/"):
        path = path.rstrip("/")
    out = urlunsplit(("https", host, path, urlencode(query), ""))
    return out[:MAX_URL_LEN]
