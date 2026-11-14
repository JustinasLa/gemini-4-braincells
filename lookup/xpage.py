"""Parse X pages as Oxylabs /scrape returns them in markdown (logged-out view).

A post page lists the thread as markdown bullet items: the posts it replies to, the post itself
(the one with a full date link, "[9:50 PM · Nov 13, 2026](/handle/status/ID)"), then replies.
A profile page has the profile header, then the account's recent posts as the same items.

Counts: X hides zero counts, so a missing number is shown by the page as nothing at all. The
post itself shows replies (link), reposts, likes, bookmarks, with views next to the date; other
items show replies (link), reposts, likes, views. Replies (a labelled link) and the post's own
views (labelled) are always safe. The unlabelled numbers are assigned only when every slot is
shown; with fewer we can't tell which count is hidden, so they all stay None (never guessed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from lookup.urls import snowflake_time

_AVATAR = re.compile(r"^\[!\[@([A-Za-z0-9_]{1,15})\]\(([^)]*)\)\]\(/([A-Za-z0-9_]{1,15})\)\s*$")
_NAME = re.compile(r"^\[(.+?)\]\(/([A-Za-z0-9_]{1,15})\)\[@(.+?)\]\(/([A-Za-z0-9_]{1,15})\)\s*$")
_STATUS_LINK = re.compile(r"\]\(/([A-Za-z0-9_]{1,15})/status/(\d+)\)")
_REL_TIME = re.compile(  # noqa: E501
    r"^\[(\d+[smhd]|[A-Z][a-z]{2} \d{1,2}(?:, \d{4})?)\]\(/([A-Za-z0-9_]{1,15})/status/(\d+)\)\s*$"
)  # noqa: E501
_DATE_LINK = re.compile(
    r"^\[(\d{1,2}:\d{2} [AP]M · [A-Z][a-z]{2} \d{1,2}, \d{4})\]"
    r"\(/([A-Za-z0-9_]{1,15})/status/(\d+)\)"
)  # noqa: E501
_VIEWS_INLINE = re.compile(r"·\[([\d.,]+[KMB]?)\s*$")
_REPLY_LINK = re.compile(r"^\[([\d.,]+[KMB]?)\]\(/compose/post\?in_reply_to=(\d+)\)\s*$")
_NUMBER = re.compile(r"^([\d.,]+[KMB]?)\s*$")
_MEDIA = re.compile(
    r"!\[[^\]]*\]\((https://pbs\.twimg\.com/(?:media|amplify_video_thumb|ext_tw_video_thumb|tweet_video_thumb)/[^)\s]+)\)"
)  # noqa: E501
_CARD = re.compile(r"^\[!\[([^\]]*)\]\((https://pbs\.twimg\.com/card_img/[^)]+)\)\s*$")
_CARD_END = re.compile(
    r"^(.*?)\]\((https?://t\.co/\w+)\)(?:\[From ([^\]]+)\]\(https?://t\.co/\w+\))?\s*$"
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_TITLE = re.compile(r'^# (.+?) on X: "(.*)"\s*$', re.S)


def unavailable_reason(md: str) -> str | None:
    """Read X's explicit notices outside post items."""
    notices = {
        "This post is from a suspended account.": "suspended",
        "Account suspended": "suspended",
        "This post is from a protected account.": "protected",
        "These posts are protected": "protected",
        "You're unable to view this Post because this account owner limits "
        "who can view their Posts.": "protected",
        "We're unable to show this content": "unavailable",
        "The content may be private, deleted or only available on the app.": "unavailable",
        "Hmm...this page doesn't exist. Try searching for something else.": "unavailable",
        "This account doesn't exist": "not_found",
    }
    in_item = False
    in_post = False
    for raw in md.splitlines():
        if raw.startswith("* [![@"):
            in_item = True
            continue
        if in_item:
            if raw.startswith("  ") or not raw.strip():
                continue
            in_item = False
        if _AVATAR.match(raw):
            in_post = True
        if in_post:
            if not raw.startswith("#"):
                continue
            in_post = False
        line = raw.strip().removeprefix("* ").lstrip("# ").replace("’", "'")
        line = line.split(" [Learn more](", 1)[0]
        if line == "This Post is from a suspended account.":
            line = "This post is from a suspended account."
        if line in notices:
            return notices[line]
    return None


def page_unavailable_reason(md: str, url: str) -> str | None:
    """X's notice for the page at `url` itself. A post page counts only when its own post is
    missing (a reply from a suspended account says nothing about the post); a profile page only
    from the "## " notice headings above its timeline (a timeline item can be another account's
    tombstone, and the name and bio are the account's own words)."""
    m = re.search(r"/status(?:es)?/(\d+)", urlsplit(url).path)
    if m:
        return None if parse_post_page(md, m.group(1)).main else unavailable_reason(md)
    header = md.split("\n* ", 1)[0].splitlines()
    return unavailable_reason("\n".join(ln for ln in header if ln.startswith("## ")))


def unavailable_tag(reason: str) -> str:
    """A few words for the verdict screen."""
    return {
        "suspended": "Account suspended",
        "protected": "Protected account",
        "unavailable": "Post not available",
        "not_found": "Account doesn't exist",
    }[reason]


def unavailable_detail(reason: str) -> str:
    """Plain wording for an unreadable X post."""
    return {
        "suspended": "X shows this post is from a suspended account",
        "protected": "X shows this post is from a protected account "
        "(only approved followers can see it)",
        "unavailable": "X says this post isn't available (it may be private, deleted or app-only)",
        "not_found": "X says this account doesn't exist",
    }[reason]


def parse_count(s: str | None) -> int | None:
    """'22.3K' -> 22300, '2.9M' -> 2900000, '1,284' -> 1284."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(s[-1:], 1)
    try:
        return int(round(float(s[:-1] if mult > 1 else s) * mult))
    except ValueError:
        return None


def _clean_text(lines: list[str]) -> str:
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln in ("Show more", "GIF", "Made with AI", "Quote", "00:00"):
            continue
        ln = _MD_LINK.sub(lambda m: m.group(1), ln).replace("\\_", "_").replace("\\*", "*")
        out.append(ln.removesuffix(" Show more"))
    return "\n".join(out).strip()


@dataclass
class XItem:
    handle: str
    name: str | None
    status_id: str | None
    text: str
    avatar_url: str | None = None
    is_main: bool = False  # the page's own post (has the full date link)
    quoted_handle: str | None = None
    quoted_id: str | None = None
    quoted_text: str | None = None
    replies: int | None = None
    reposts: int | None = None
    likes: int | None = None
    bookmarks: int | None = None
    views: int | None = None
    media_urls: list[str] = field(default_factory=list)
    card_title: str | None = None
    card_url: str | None = None
    card_domain: str | None = None
    card_image: str | None = None
    counts_shown: int = 0  # unlabelled numbers the page showed (fewer than slots = ambiguous)

    @property
    def created_at(self):
        return snowflake_time(self.status_id) if self.status_id else None


def _items_block(md: str) -> list[list[str]]:
    """Split the markdown's bullet list ('* [![@h]...') into per-item lines, indent removed."""
    items: list[list[str]] = []
    cur: list[str] | None = None
    for raw in md.splitlines():
        if raw.startswith("* [![@"):
            cur = [raw[2:]]
            items.append(cur)
        elif cur is not None:
            if raw.startswith("  ") or not raw.strip():
                cur.append(raw[2:] if raw.startswith("  ") else "")
            else:
                cur = None
    return items


def _parse_item(lines: list[str]) -> XItem | None:
    m = _AVATAR.match(lines[0].strip())
    if not m:
        return None
    handle, avatar = m.group(3), m.group(2)
    name = None
    i = 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and (n := _NAME.match(lines[i].strip())):
        name = n.group(1)
        i += 1
    item = XItem(handle=handle, name=name, status_id=None, text="", avatar_url=avatar)
    own: list[str] = []
    quoted: list[str] = []
    target = own
    numbers: list[str] = []
    reply_seen = False
    in_card = False
    while i < len(lines):
        ln = lines[i].strip()
        i += 1
        if not ln:
            continue
        if q := _AVATAR.match(ln):  # a quoted post starts
            item.quoted_handle = q.group(3)
            target = quoted
            # skip its name line
            if i < len(lines) and _NAME.match(lines[i].strip()):
                i += 1
            elif i + 1 < len(lines) and _NAME.match(lines[i + 1].strip()):
                i += 2
            continue
        if r := _REL_TIME.match(ln):
            if target is quoted and r.group(2).lower() == (item.quoted_handle or "").lower():
                item.quoted_id = r.group(3)
            elif r.group(2).lower() == handle.lower() and not item.status_id:
                item.status_id = r.group(3)
            continue
        if d := _DATE_LINK.match(ln):
            if d.group(2).lower() == handle.lower():
                item.status_id = d.group(3)
                item.is_main = True
                if v := _VIEWS_INLINE.search(ln):
                    item.views = parse_count(v.group(1))
            target = None  # text is over
            continue
        if ln.startswith("Views]("):
            continue
        if rl := _REPLY_LINK.match(ln):
            item.replies = parse_count(rl.group(1))
            reply_seen = True
            target = None
            continue
        if _NUMBER.match(ln) and (target is None or reply_seen or _looks_like_tail(lines, i)):
            numbers.append(ln)
            target = None
            continue
        if c := _CARD.match(ln):
            in_card = True
            item.card_image = c.group(2)
            item.card_title = c.group(1) or None
            continue
        if in_card:
            if ce := _CARD_END.match(ln):
                item.card_title = ce.group(1).strip() or item.card_title
                item.card_url = ce.group(2)
                item.card_domain = (ce.group(3) or "").removeprefix("From ").strip() or None
                in_card = False
            continue
        for mu in _MEDIA.findall(ln):
            if mu not in item.media_urls:
                item.media_urls.append(mu)
        if ln.startswith("![") or ln.startswith("[!["):
            continue
        if target is not None:
            target.append(ln)
    item.text = _clean_text(own)
    item.quoted_text = _clean_text(quoted) or None
    counts = [parse_count(x) for x in numbers]
    slots = ("reposts", "likes", "bookmarks") if item.is_main else ("reposts", "likes", "views")
    if len(counts) == len(slots):  # all shown: positions are unambiguous
        for name_, val in zip(slots, counts, strict=True):
            setattr(item, name_, val)
    item.counts_shown = len(counts)
    return item


def _looks_like_tail(lines: list[str], i: int) -> bool:
    """A bare number is a count only if nothing but numbers follows it in the item."""
    return all(not ln.strip() or _NUMBER.match(ln.strip()) for ln in lines[i:])


def _unescape(md: str) -> str:
    """Markdown escapes inside handles ('@alb\\_olmos') would break the handle patterns."""
    return md.replace("\\_", "_")


def parse_items(md: str) -> list[XItem]:
    md = _unescape(md)
    out = []
    for block in _items_block(md):
        it = _parse_item(block)
        if it is not None and it.status_id:
            out.append(it)
    return out


def title_text(md: str) -> tuple[str | None, str | None]:
    """('Steve Lookner', 'full post text') from the page title line, if present."""
    for ln in _unescape(md).splitlines():
        if ln.startswith("# ") and " on X: " in ln:
            m = _TITLE.match(ln)
            if m:
                text = re.sub(r"\s*https://t\.co/\w+\s*$", "", m.group(2)).strip()
                return m.group(1), text
    return None, None


@dataclass
class PostPage:
    main: XItem | None
    ancestors: list[XItem]  # posts the main post replies to, oldest first
    replies: list[XItem]


def parse_post_page(md: str, status_id: str) -> PostPage:
    items = parse_items(md)
    idx = next((k for k, it in enumerate(items) if it.status_id == status_id), None)
    if idx is None:
        return PostPage(None, [], [])
    main = items[idx]
    _, full = title_text(md)
    if full and len(full) > len(main.text) and not main.quoted_text:
        main.text = full  # timeline items can be truncated ("... Show more"); the title isn't
    seen = {main.status_id}
    ancestors, replies = [], []
    for it in items[:idx]:
        if it.status_id not in seen:
            seen.add(it.status_id)
            ancestors.append(it)
    for it in items[idx + 1 :]:
        if it.status_id not in seen:
            seen.add(it.status_id)
            replies.append(it)
    return PostPage(main, ancestors, replies)


@dataclass
class Profile:
    handle: str
    name: str | None = None
    bio: str | None = None
    location: str | None = None
    joined: str | None = None  # "2009-06"
    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None
    avatar_url: str | None = None
    recent: list[XItem] = field(default_factory=list)


_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}


def parse_profile(md: str, handle: str) -> Profile:
    p = Profile(handle=handle)
    md = _unescape(md)
    lines = [ln.strip() for ln in md.split("* [![@", 1)[0].splitlines()]
    for k, ln in enumerate(lines):
        if m := re.match(r"^([\d.,]+[KMB]?) posts$", ln):
            p.posts_count = parse_count(m.group(1))
        elif m := re.match(r"^\[Joined ([A-Z][a-z]+) (\d{4})\]", ln):
            mon = _MONTHS.get(m.group(1))
            p.joined = f"{m.group(2)}-{mon:02d}" if mon else m.group(2)
        elif "Following](/" in ln or "Followers](/" in ln:
            if m := re.search(r"\[([\d.,]+[KMB]?) Following\]", ln):
                p.following = parse_count(m.group(1))
            if m := re.search(r"\[([\d.,]+[KMB]?) Followers\]", ln):
                p.followers = parse_count(m.group(1))
        elif ln.startswith("# ") and p.name is None:
            p.name = ln[2:].strip()
            # bio / location follow "@handle"
            rest = [x for x in lines[k + 1 : k + 12] if x]
            if rest and rest[0].lstrip("@").replace("\\_", "_").lower() == handle.lower():
                rest = rest[1:]
            bio = []
            for x in rest:
                if x.startswith("[Joined") or "Following](" in x or x.startswith("[!["):
                    break
                if re.match(r"^\[[^\]]+\]\(https://t\.co/\w+\)$", x):
                    continue
                bio.append(x)
            if bio:
                p.bio = _clean_text(bio[:1])
                if len(bio) > 1 and len(bio[-1]) <= 40:
                    p.location = _clean_text([bio[-1]])
        elif m := re.match(
            r"^\[!\[@[^\]]+\]\((https://pbs\.twimg\.com/profile_images/[^)]+)\)\]\(/[^)]+/photo\)",
            ln,
        ):  # noqa: E501
            p.avatar_url = m.group(1)
    p.recent = [it for it in parse_items(md) if it.handle.lower() == handle.lower()]
    return p
