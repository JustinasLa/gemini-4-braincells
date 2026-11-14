"""Is a picture group safe to show on the public wall?

A group is NOT safe when any of these holds:
- a post carrying the picture has an adult hashtag or adult words in its text (word list below);
- Bluesky's AppView labels a representative post or its author porn / sexual / nudity /
  graphic-media (and the related gore, nsfl, sexual-figurative, or the moderation label !hide),
  either from Bluesky's moderation service or the author's own self-label;
- the vision model, when it read the picture, flagged it adult or graphic, or the text it read on
  the picture has adult words.

A group is also kept off the wall, without a reason that sticks ("held"), while its representative
posts could not be checked or are missing from the AppView (deleted or taken down), and while the
vision model has not read its current picture (swarm/live.py).

Unsafe groups are still real swarms: they are stored and scored, but the wall never lists them and
their detail page is a 404. The rule is deliberately one-sided: hiding a harmless meme costs little,
showing porn on a public page costs a lot.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

GET_POSTS = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"
UA = {"User-Agent": "noise-radar-swarm/0.1 (read-only research)"}
ADULT_LABELS = frozenset(
    {"porn", "sexual", "nudity", "graphic-media", "gore", "nsfl", "sexual-figurative", "!hide"}
)

# In free text: words that are hardly ever used outside adult content.
_ADULT_TEXT = re.compile(
    r"(?<![\w])(?:nsfw|nsfl|porn\w*|p0rn\w*|nudes|nudity|onlyfans|0nlyfans|fansly|xxx+"
    r"|hentai|sexting|camgirls?|cam\s?girls?|r-?18|milfs?|"
    r"18\s?\+|\+\s?18|sex\s?(?:cam|chat|video|tape)s?|adult\s?(?:content|video)s?)(?![\w])",
    re.I,
)
# Hashtags (stored lowercased, without '#'): the text list plus tags that are adult as a tag.
_ADULT_TAG = re.compile(
    r"^(?:nsfw\w*|nsfl|porn\w*|nude\w*|nudity|naked|onlyfans\w*|fansly|xxx+|hentai|escorts?"
    r"|sex|sexy|sexting|horny\w*|boobs?|tits|ass|milfs?|camgirls?|lewds?|r18|18\+|18plus|adult"
    r"|adultcontent|nsfwart|furryporn|gayporn|bdsm|fetish\w*|kink\w*|thirsttrap\w*)$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Safety:
    safe: bool
    reason: str = ""  # empty when safe: "tag #nsfw", "label porn", "vision: adult"


def text_marker(text: str | None, tags: Iterable[str] | None = None) -> str | None:
    """The first adult marker in a post's text or hashtags, or None."""
    for tag in tags or ():
        t = (tag or "").lstrip("#").strip()
        if t and _ADULT_TAG.match(t):
            return f"tag #{t.lower()}"
    if text:
        m = _ADULT_TEXT.search(text)
        if m:
            return f"text {m.group(0).lower()!r}"
        for tag in re.findall(r"#(\w+)", text):
            if _ADULT_TAG.match(tag):
                return f"tag #{tag.lower()}"
    return None


def _label_values(obj: dict | None) -> set[str]:
    if not isinstance(obj, dict):
        return set()
    out = {lab.get("val") for lab in obj.get("labels") or [] if isinstance(lab, dict)}
    rec_labels = (
        (obj.get("record") or {}).get("labels") if isinstance(obj.get("record"), dict) else None
    )
    if isinstance(rec_labels, dict):  # self-labels on the record
        out |= {v.get("val") for v in rec_labels.get("values") or [] if isinstance(v, dict)}
    return {v for v in out if isinstance(v, str)}


class LabelResult(dict):
    """{post uri: label values} for exactly the posts the AppView returned. `failed`: uris whose
    request failed (not checked). A uri in neither was checked but not returned: deleted or taken
    down. Callers must treat both kinds of absence as "not known to be safe"."""

    def __init__(self, *args, failed: set[str] | None = None, **kw) -> None:
        super().__init__(*args, **kw)
        self.failed: set[str] = set(failed or ())


def fetch_labels(uris: list[str], client: httpx.Client | None = None) -> LabelResult:
    """Label values on each post and on its author from the public AppView (see LabelResult).

    Network errors and non-200 answers never raise: the chunk's uris go to `failed`.
    """
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), headers=UA)
    out = LabelResult()
    try:
        for i in range(0, len(uris), 25):
            chunk = uris[i : i + 25]
            try:
                r = client.get(GET_POSTS, params=[("uris", u) for u in chunk])
                if r.status_code != 200:
                    log.warning("getPosts HTTP %s", r.status_code)
                    out.failed.update(chunk)
                    continue
                posts = r.json().get("posts") or []
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("getPosts failed: %s", exc)
                out.failed.update(chunk)
                continue
            asked = set(chunk)
            for post in posts:
                if not isinstance(post, dict) or post.get("uri") not in asked:
                    continue
                out[post["uri"]] = _label_values(post) | _label_values(post.get("author"))
    finally:
        if own:
            client.close()
    return out


def judge(
    posts: Iterable[tuple[str | None, Iterable[str] | None]],
    labels: Iterable[set[str]] = (),
    *,
    vision_adult: bool | None = None,
) -> Safety:
    """posts: (text, tags) of every post carrying the picture; labels: AppView label sets of the
    representative posts that were checked."""
    if vision_adult:
        return Safety(False, "vision: adult")
    for found in labels:
        bad = sorted(found & ADULT_LABELS)
        if bad:
            return Safety(False, f"label {bad[0]}")
    for text, tags in posts:
        marker = text_marker(text, tags)
        if marker:
            return Safety(False, marker)
    return Safety(True)


# --- the vision read (optional) -------------------------------------------------------------------

WALL_PROMPT = """You look at one picture that many Bluesky accounts posted. It may be shown as a
thumbnail on a public, general-audience web page about coordinated posting.

Return only one JSON object with keys:
- kind: one of "meme", "ad", "photo", "screenshot", "other" (ad = it promotes a product, service,
  shop, channel or account).
- caption: the legible text on the picture, verbatim, at most 300 characters ("" if none).
- adult: true if the picture is nude, sexual OR sexually suggestive: posing in lingerie,
  underwear or swimwear, a focus on breasts, buttocks or crotch, a provocative selfie, or an
  escort, massage or dating ad with such imagery. false otherwise.
- graphic: true if it shows gore, injury or a dead body.
- reason: at most 8 words on why adult or graphic is true ("" otherwise).
Do not describe bodies or people. When unsure whether it is suggestive, answer adult: true."""

WALL_KINDS = ("meme", "ad", "photo", "screenshot", "other")


class WallReader:
    """Vision read for the wall on the Nemotron Super VL endpoint (media/ uses the same one):
    kind, caption and a stricter adult flag than media/'s evidence prompt (which only flags
    nudity and sex: swimwear-selfie bait networks pass it). Returns (kind, caption, adult)."""

    def __init__(self, api_key: str, *, base_url: str, model: str, client=None, timeout_s=30.0):
        from openai import OpenAI

        self.model = model
        self._client = client or OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )

    @classmethod
    def from_settings(cls, settings) -> WallReader | None:
        if settings.verda_inference_key:
            return cls(
                settings.verda_inference_key,
                base_url=settings.verda_base_url,
                model=settings.verda_model,
            )
        if settings.nvidia_api_key:
            return cls(
                settings.nvidia_api_key,
                base_url=settings.nvidia_base_url,
                model=settings.nvidia_media_model,
            )
        return None

    def __call__(self, data: bytes) -> tuple[str | None, str | None, bool]:
        """Only the picture goes to the model: post text is written by the accounts being judged
        and could steer the answer."""
        import base64

        from media.nemotron import NotVisionModelError, is_super_vl_model, parse_content

        url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": WALL_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read this picture and return the JSON object."},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=700,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        returned = getattr(response, "model", None)
        if not is_super_vl_model(returned):
            raise NotVisionModelError(f"vision read answered by {returned!r}, not Super VL")
        choice = response.choices[0]
        payload = parse_content(choice.message.content, truncated=choice.finish_reason == "length")
        kind = payload.get("kind") if payload.get("kind") in WALL_KINDS else "other"
        caption = str(payload.get("caption") or "").strip()[:300] or None
        adult = payload.get("adult") is not False or payload.get("graphic") is True
        if not adult and caption and text_marker(caption):
            adult = True  # adult words written on the picture
        return kind, None if adult else caption, adult
