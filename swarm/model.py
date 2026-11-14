"""The meme swarm contract: what goes into the scorer and the report that comes out.

A *meme* here is one picture and the posts that carry it. The scorer asks one question about
that set of posts: do the accounts behind them act like a swarm (bot network, astroturfing,
brigading) or like people who each found the picture funny? It never judges a single post or a
single account, and a picture being popular is not evidence: the signals are about *how* it was
posted (timing, how alike the accounts are, reused words and files, who boosts whom).

These models are the JSON served by the API and read by Trackdown. Change a field only together
with web/trackdown/src/data/swarm.ts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["bluesky", "x", "web"]
Family = Literal["timing", "accounts", "copy", "boost"]
# swarm -> "Shill", organic -> "Chill", unclear -> "Can't tell". A verdict describes the group of
# posts, never one account.
Verdict = Literal["swarm", "organic", "unclear"]
# live: every post of this picture our Bluesky firehose saw in the window (complete for the
# window). sample: what a search returned for a lookup (a sample; copies are missing).
Basis = Literal["live", "sample"]

FAMILIES: tuple[Family, ...] = ("timing", "accounts", "copy", "boost")


class Sighting(BaseModel):
    """One action that carries or boosts the picture."""

    id: str  # post uri / "x:<id>" / page url: unique within the meme
    platform: Platform
    account: str  # stable account key: did, or "x:<handle lowercased>"
    at: datetime | None = None
    # False when `at` is a search-engine hint or a crawl date: left out of timing signals
    time_exact: bool = True
    # post: the account posted the picture itself. repost/quote/reply/like: it boosted `target`.
    kind: Literal["post", "repost", "quote", "reply", "like"] = "post"
    target: str | None = None  # the Sighting.id that was boosted (kind != "post")
    text: str = ""  # the words posted with the picture
    url: str | None = None
    # the exact file: Bluesky blob CID or X media key. Equal keys = the same upload bytes.
    media_key: str | None = None
    langs: list[str] = Field(default_factory=list)
    # public counts on the post, when the platform shows them
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None


class AccountInfo(BaseModel):
    """What is known about an account. Every field may be missing: a signal that needs a field
    counts only the accounts that have it and says how many that was."""

    key: str  # Sighting.account
    platform: Platform = "bluesky"
    handle: str | None = None
    name: str | None = None
    created_at: datetime | None = None
    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None
    bio: str | None = None
    has_avatar: bool | None = None
    host: str | None = None  # Bluesky PDS host


class SwarmSignal(BaseModel):
    family: Family
    name: str  # snake_case, stable: "burst", "same_text", ...
    score: float  # 0..1
    # for: points to a swarm. against: points to ordinary people (an organic guard).
    polarity: Literal["for", "against"] = "for"
    # a few plain words with the numbers, shown as a chip: "31 accounts in 4 min"
    summary: str
    numbers: dict[str, float | int | str | None] = Field(default_factory=dict)
    # Sighting ids that show it (<= 12), for links. Only timing and copy signals that point to a
    # swarm carry them: accounts and boost examples would single out accounts.
    examples: list[str] = Field(default_factory=list)


class Burst(BaseModel):
    """The densest stretch of posting."""

    start: datetime
    end: datetime
    n_posts: int
    n_accounts: int


class TimelinePoint(BaseModel):
    """One sighting for the dot timeline (<= 300, evenly sampled, in time order)."""

    t: float  # epoch seconds
    a: int  # index of the account within this report (0 = first to post); not a name
    k: Literal["post", "repost", "quote", "reply", "like"] = "post"
    # posts with the same number share the same words (0 = own words or no words)
    g: int = 0
    burst: bool = False


class SwarmReport(BaseModel):
    verdict: Verdict = "unclear"
    # why it is unclear, in a few words ("only 3 accounts"); empty otherwise
    why_unclear: str = ""
    score: float = 0.0  # 0..1, noisy-OR of the two strongest families
    guard: float = 0.0  # 0..1, strongest organic guard
    p_value: float | None = None  # the timing test, when it ran
    families: dict[Family, float] = Field(default_factory=dict)
    fired: list[Family] = Field(default_factory=list)
    signals: list[SwarmSignal] = Field(default_factory=list)  # strongest first, guards last
    n_posts: int = 0  # sightings of kind "post"
    n_boosts: int = 0  # the other kinds
    n_accounts: int = 0  # distinct accounts that posted the picture
    n_accounts_known: int = 0  # of those, with any AccountInfo
    first_at: datetime | None = None
    last_at: datetime | None = None
    burst: Burst | None = None
    timeline: list[TimelinePoint] = Field(default_factory=list)
    basis: Basis = "sample"
    # one short line on what the data covers: "43 posts our Bluesky feed saw in 24 h"
    basis_note: str = ""
    version: str = ""


class SwarmPost(BaseModel):
    """A post shown under a swarm (a link, never a label on its author)."""

    url: str
    platform: Platform = "bluesky"
    handle: str | None = None
    at: datetime | None = None
    text: str = ""


class SwarmCard(BaseModel):
    """One picture on the wall (GET /api/swarms)."""

    id: str  # stable id of the picture group
    thumb: str  # https image URL, safe to hotlink
    verdict: Verdict
    score: float
    n_posts: int
    n_accounts: int
    first_at: datetime | None = None
    last_at: datetime | None = None
    families: dict[Family, float] = Field(default_factory=dict)
    # the strongest signal's summary: "43 accounts · same words"
    headline: str = ""
    # what the picture is, when read: meme, ad, photo, screenshot, other
    kind: str | None = None


class SwarmDetail(SwarmCard):
    """GET /api/swarms/{id}."""

    picture: str  # full-size image URL
    report: SwarmReport
    posts: list[SwarmPost] = Field(default_factory=list)  # <= 30, earliest first
    caption: str | None = None  # the text on the picture, when read


class SwarmLiveRef(BaseModel):
    """A pointer from a lookup to the live Bluesky group of the same picture (its page is
    /?swarm=<id>, its data GET /api/swarms/{id}). Small on purpose: lookups are polled."""

    id: str
    verdict: Verdict
    n_accounts: int


class SwarmWall(BaseModel):
    items: list[SwarmCard] = Field(default_factory=list)
    as_of: datetime | None = None
    window_h: float | None = None
