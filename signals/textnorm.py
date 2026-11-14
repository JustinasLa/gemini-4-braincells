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
_NON_WORD_RE = re.compile(r"[^\w#]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)
# Scripts written without spaces between words. UNSPACED_RE (used for the web-search phrase,
# on raw text) covers Thai, Lao, Myanmar, Khmer, kana incl. half-width, CJK ideographs.
UNSPACED_CHARS = (
    "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u31f0-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\U00020000-\U0003134f"
)
UNSPACED_RE = re.compile(f"[{UNSPACED_CHARS}]")
# tokens() splits only kana and CJK ideographs per character. Thai, Lao, Myanmar and Khmer are
# left as they were: normalize_text drops their vowel and tone marks (not \w), so one token per
# remaining character would compare consonant skeletons. (NFKC has already widened half-width
# kana here.)
_CJK_CHARS = (
    "\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
)
_CJK_RE = re.compile(f"[{_CJK_CHARS}]")
_CJK_PIECE_RE = re.compile(f"[{_CJK_CHARS}]|[^{_CJK_CHARS}]+")

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
    """Words of a normalised text. Kana and CJK ideographs (Japanese, Chinese) are one token per
    character; words of every other script are unchanged."""
    if not norm_text:
        return []
    out: list[str] = []
    for w in norm_text.split():
        if _CJK_RE.search(w):
            out.extend(_CJK_PIECE_RE.findall(w))
        else:
            out.append(w)
    return out


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


def url_domain(normalized_url: str) -> str:
    """Host part of a normalised URL (e.g. 'example.com')."""
    return urlsplit(normalized_url).hostname or ""
