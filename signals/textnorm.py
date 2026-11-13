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

# Query parameters that only track clicks; dropping them makes the same link compare equal.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "_ga", "_gl",
    "ref", "ref_src", "ref_url", "si", "feature", "share", "s", "t", "smid", "cmpid",
    "yclid", "twclid", "ttclid", "spm", "src", "via",
}  # fmt: skip


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


def text_hash(text: str | None) -> str | None:
    """Stable 16-hex hash of the normalised text, or None when nothing is left after normalising."""
    norm = normalize_text(text)
    if not norm:
        return None
    return hashlib.blake2b(norm.encode(), digest_size=8).hexdigest()


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
    return urlunsplit(("https", host, path, urlencode(query), ""))


def url_domain(normalized_url: str) -> str:
    """Host part of a normalised URL (e.g. 'example.com')."""
    return urlsplit(normalized_url).hostname or ""
