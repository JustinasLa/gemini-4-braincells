"""Screenshot read: Nemotron Super VL reads a screenshot of a social-media post as fields.

Only for images the main read already typed `media_type="screenshot"`. The result
(`ScreenshotPost`) is what later code (lookup/receipts.py, "find the original") uses to find the
real post and compare the screenshot against it. A screenshot that isn't of a post (a chat, an
article, a settings page) reads as None.

`ScreenshotPost` is a contract shared with lookup/. Don't rename or drop fields without a note
in the PR that changes them.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCREENSHOT_PROMPT_VERSION = "screenshot-v1"

PLATFORMS = ("x", "bluesky", "threads", "facebook", "instagram", "tiktok", "other", "unknown")
COUNT_KEYS = ("replies", "reposts", "quotes", "likes", "views", "bookmarks")

# Names the model may use for a platform or a count, mapped to the contract's vocabulary.
_PLATFORM_ALIASES = {
    "twitter": "x",
    "x (twitter)": "x",
    "twitter/x": "x",
    "x/twitter": "x",
    "x.com": "x",
    "twitter.com": "x",
    "bsky": "bluesky",
    "bsky.app": "bluesky",
    "blue sky": "bluesky",
    "threads.net": "threads",
    "meta threads": "threads",
    "fb": "facebook",
    "ig": "instagram",
    "insta": "instagram",
}
_COUNT_ALIASES = {
    "reply": "replies",
    "comments": "replies",
    "comment": "replies",
    "repost": "reposts",
    "retweets": "reposts",
    "retweet": "reposts",
    "quote": "quotes",
    "quote_posts": "quotes",
    "quote_tweets": "quotes",
    "like": "likes",
    "hearts": "likes",
    "favorites": "likes",
    "favourites": "likes",
    "view": "views",
    "impressions": "views",
    "bookmark": "bookmarks",
    "saves": "bookmarks",
}
_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_COUNT_RE = re.compile(r"^(\d+(?:[., ]\d+)*)\s*([kmb])?(?:\s*(?:views?|likes?|replies|reposts?))?$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")
# A UI-oddity note must describe the image, never the people (shared public evidence wording).
_PEOPLE_CLAIMS = re.compile(
    r"\b(bots?|shill\w*|spammer\w*|scammer\w*|trolls?|fake accounts?|sock ?puppet\w*)\b", re.I
)
# The model judges dates against its own training cutoff ("the date is in the future"); a date
# newer than the model is not a sign of editing.
_DATE_JUDGEMENT = re.compile(r"\b(future|not yet happened|hasn'?t happened)\b", re.I)


def judges_date(note: str) -> bool:
    """A model note that judges a shown date against the model's own training cutoff ("a future
    date suggests a mock-up"): not evidence about the image, so callers drop it."""
    return bool(_DATE_JUDGEMENT.search(note or ""))


_MAX_SHORT = 120

SCREENSHOT_SYSTEM_PROMPT = f"""You read one image that was already classified as a screenshot.
Decide whether it shows a single social-media post (the post itself, as the app or website shows
it) and, if so, transcribe that post's visible fields so the original can be looked up later.

Rules:
- is_post: true only if the image shows a social-media post with its author line (name or
  handle) and/or its text in a platform's post UI. False for chats/DMs, news articles, profile
  pages without a post, search results, settings, documents, memes without post UI.
- If several posts are visible (a thread, replies, a quote), read the main one: the post the
  screenshot is centred on (usually the largest, with the full timestamp and counts). Do not
  copy replies, the quoted post, ads, trends or other UI chrome into text.
- platform: from the UI look, one of {list(PLATFORMS)}. A platform not in that list (for
  example Truth Social, Reddit, LinkedIn, Mastodon) is "other", never "x". "unknown" if you
  cannot tell.
- display_name, handle: as shown. handle without the leading @. null if not legible.
- shown_time: the time or date exactly as shown ("3:14 PM · Nov 13, 2026", "2h"), else null.
- text: the post's own text, verbatim, keeping line breaks as \\n. null if none is legible.
  Text inside an image or video attached to the post is not the post's text.
  Never invent or complete unreadable words.
- counts: replies, reposts, quotes, likes, views, bookmarks as shown ("2.5K", "1,204", 37).
  null for a count that is not shown. Never write 0 for a count that is not shown.
- ui_oddities: visible signs the screenshot was edited or is not genuine: mixed fonts or
  sizes in one text block, misaligned or overlapping text, an odd or misplaced badge, UI that
  does not match the platform, impossible numbers. Be cautious: usually an empty list. Never
  judge a date or time against your own knowledge: a date you think is in the future is normal.
  Describe the image only. Never call a person or account a bot, shill or spammer.
- confidence: 0.0-1.0, how sure you are that the fields are read correctly.

Return only one JSON object with keys: is_post, platform, display_name, handle, shown_time,
text, counts (object with the six keys), ui_oddities (array of strings), confidence."""

SCREENSHOT_USER_PROMPT = "Read this screenshot and return the JSON object."


@dataclass(frozen=True, slots=True)
class ScreenshotPost:
    media_hash: str
    platform: str  # x | bluesky | threads | facebook | instagram | tiktok | other | unknown
    display_name: str | None
    handle: str | None  # without the leading @, None if not legible
    shown_time: str | None  # verbatim as shown ("3:14 PM · Nov 13, 2026", "2h")
    text: str | None  # the post's own text, verbatim; not replies/UI chrome
    counts: dict[str, int | None]  # COUNT_KEYS; None = not shown (never guessed 0)
    ui_oddities: list[str]  # visible signs of editing; cautious, often empty
    confidence: float  # 0..1
    model: str  # model id the response returned
    latency_ms: int
    prompt_version: str = field(default=SCREENSHOT_PROMPT_VERSION, compare=False)

    def to_detail(self) -> dict[str, Any]:
        """JSON-ready dict for `evidence.detail.screenshot`."""
        return asdict(self)

    @classmethod
    def from_detail(cls, detail: dict[str, Any]) -> ScreenshotPost:
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: detail[k] for k in names if k in detail})


def parse_count(value: Any) -> int | None:
    """'2.5K' -> 2500, '1.2M' -> 1200000, '1,204' -> 1204, 37 -> 37. None when not a count."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):  # only a whole number: 0.4 must not become a shown 0
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else None
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace(" ", " ").replace(" ", " ")
    m = _COUNT_RE.match(s)
    if not m:
        return None
    number, suffix = m.groups()
    if suffix:
        # "2.5K" / "2,5K" (decimal comma); a suffix count never has thousands separators
        if number.count(".") + number.count(",") > 1 or " " in number:
            return None
        scaled = float(number.replace(",", ".")) * _SUFFIX[suffix]
        return round(scaled) if math.isfinite(scaled) else None
    # no suffix: "1,204" / "1.204" / "1 204" are thousands separators, "12" is a plain integer
    parts = re.split(r"[., ]", number)
    if len(parts) > 1 and not all(len(p) == 3 for p in parts[1:]):
        return None  # "1.5" without a suffix is not a count the UI shows
    try:
        return int("".join(parts))
    except ValueError:  # over Python's int() digit limit (4300): a degenerate reply, not a count
        return None


def normalise_counts(raw: Any) -> dict[str, int | None]:
    """Every key in COUNT_KEYS; unknown keys dropped; unreadable or missing values None."""
    out: dict[str, int | None] = dict.fromkeys(COUNT_KEYS)
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        k = re.sub(r"[\s\-]+", "_", str(key).strip().lower())
        k = _COUNT_ALIASES.get(k, k)
        if k in out and out[k] is None:
            out[k] = parse_count(value)
    return out


def normalise_platform(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    p = value.strip().lower()
    p = _PLATFORM_ALIASES.get(p, p)
    return p if p in PLATFORMS else "other"


def normalise_handle(value: Any) -> str | None:
    """'@Name.bsky.social' -> 'Name.bsky.social'. None if missing or not handle-shaped."""
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("@").strip()
    return h if _HANDLE_RE.match(h) else None


def _text(value: Any, limit: int | None = None) -> str | None:
    """Stripped string or None. Only JSON null is "not shown": a post may literally say "None"."""
    if not isinstance(value, str):
        return None
    s = value.replace("\r\n", "\n").strip()
    return (s[:limit] if limit else s) or None


def _oddities(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = _text(v, 200)
        if not s or s.lower() in {"none", "no oddities"}:
            continue
        if not (_PEOPLE_CLAIMS.search(s) or _DATE_JUDGEMENT.search(s)):
            out.append(s)
    return list(dict.fromkeys(out))[:8]


def _is_post(value: Any) -> bool:
    """The reply's is_post. Missing or unreadable raises ValueError: a malformed reply must take
    the retry path, not be stored as a permanent "not a post"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no"}:
        return False
    raise ValueError(f"is_post must be true or false, got {value!r}")


def screenshot_from_payload(
    payload: dict[str, Any], *, media_hash: str, model: str, latency_ms: int
) -> ScreenshotPost | None:
    """Normalise a parsed model reply. None when it isn't a post, or nothing findable is legible
    (no handle, display name or text). Raises ValueError when is_post is missing or invalid."""
    if not _is_post(payload.get("is_post")):
        return None
    for key in ("display_name", "handle", "text"):  # an incomplete reply must be retried
        if key not in payload or not isinstance(payload[key], str | None):
            raise ValueError(f"{key} must be a string or null")
    display_name = _text(payload.get("display_name"), _MAX_SHORT)
    handle = normalise_handle(payload.get("handle"))
    text = _text(payload.get("text"))  # verbatim, never cut (max_tokens bounds the reply)
    if not (display_name or handle or text):
        return None
    try:
        confidence = float(payload.get("confidence", 0.5))
        confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    return ScreenshotPost(
        media_hash=media_hash,
        platform=normalise_platform(payload.get("platform")),
        display_name=display_name,
        handle=handle,
        shown_time=_text(payload.get("shown_time"), _MAX_SHORT),
        text=text,
        counts=normalise_counts(payload.get("counts")),
        ui_oddities=_oddities(payload.get("ui_oddities")),
        confidence=round(confidence, 4),
        model=model,
        latency_ms=latency_ms,
    )
