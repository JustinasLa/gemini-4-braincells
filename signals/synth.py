"""Synthetic coordinated campaigns and organic look-alikes, injected IN MEMORY into a window.

Nothing here is ever posted anywhere or written to the live database: everything is generated
locally, seeded and reproducible, and every synthetic DID starts with `did:plc:synth`.

Difficulty levels (what a campaign does inside the window):
  L1  copy-paste text + same link, brand-new templated accounts, tight burst (< 1 min),
      and the accounts co-like the same promoted posts within a minute
  L2  paraphrased text (1-2 words swapped per slot), accounts 3-8 weeks old,
      timing jittered over ~5 minutes, co-likes jittered over a few minutes
  L3  heavy paraphrase, aged accounts with followers on diverse PDSes, slow drip over the whole
      window, each account also doing unrelated organic-looking likes

Hard negatives (organic look-alikes that must NOT come out `coordinated`):
  viral       thousands of established accounts liking one post
  news_link   dozens of established accounts sharing the same news URL with their own words
  live_event  a burst of short, varied posts with the same hashtag (live TV / sports)
  fandom      established fans liking the same handful of posts at their own pace
"""

import hashlib
from dataclasses import dataclass, field

import numpy as np

from signals.score import AccountInfo
from signals.textnorm import text_hash
from signals.window import WAction, WindowData, WPost

SYNTH_PREFIX = "did:plc:synth"

_WORDS = ["amber", "nova", "pixel", "river", "echo", "lumen", "orbit", "maple", "cobalt", "delta"]
_TEMPLATE = [
    ["Huge", "Massive", "Incredible"],
    ["news:", "update:", "alert:"],
    ["the new", "this new", "the latest"],
    ["policy", "plan", "proposal"],
    ["will", "is going to", "could"],
    ["destroy", "ruin", "wreck"],
    ["our local", "your local", "every local"],
    ["economy", "jobs", "businesses"],
    ["read", "see", "check"],
    ["the full story", "the real story", "what they hide"],
    ["before it gets deleted", "before they delete it", "while you still can"],
]
_L3_SENTENCES = [
    "Not sure people realise what this policy means for small towns.",
    "Honestly the policy everyone is cheering will hurt local shops the most.",
    "Did anyone actually read the fine print on this plan? Local jobs are toast.",
    "My neighbour runs a bakery and says this proposal would finish her off.",
    "Funny how nobody on TV talks about what the plan does to local businesses.",
    "Small business owners are terrified of this, and nobody is listening.",
]
_ORGANIC_SHORT = [
    "what a goal",
    "unbelievable",
    "no way",
    "this is insane",
    "lets go",
    "i cant watch",
    "ref is blind",
    "amazing scenes",
    "wow",
    "tears",
]


def _did(tag: str, i: int, seed: int) -> str:
    h = hashlib.blake2b(f"{tag}|{seed}|{i}".encode(), digest_size=10).hexdigest()
    return f"{SYNTH_PREFIX}{tag}{h}"[:32]


def _post_uri(did: str, n: int) -> str:
    return f"at://{did}/app.bsky.feed.post/synth{n:06d}"


@dataclass
class Synthetic:
    name: str
    coordinated: bool  # ground truth label
    posts: list[WPost] = field(default_factory=list)
    actions: list[WAction] = field(default_factory=list)
    accounts: dict[str, AccountInfo] = field(default_factory=dict)

    @property
    def dids(self) -> set[str]:
        return set(self.accounts)


def _info(
    did: str,
    rng,
    age: tuple[float, float],
    followers: tuple[int, int],
    *,
    handle: str,
    avatar: bool,
    desc: int,
    pds: str,
) -> AccountInfo:
    return AccountInfo(
        did=did,
        handle=handle,
        display_name=handle.split(".")[0],
        age_days=float(rng.uniform(*age)),
        followers=int(rng.integers(*followers)),
        follows=int(rng.integers(0, 300)),
        posts_count=int(rng.integers(5, 3000)),
        description_len=desc,
        has_avatar=avatar,
        pds_host=pds,
        handle_changes=0,
    )


def campaign(level: str, wd: WindowData, seed: int = 0, n_accounts: int = 12) -> Synthetic:
    rng = np.random.default_rng(seed)
    s = Synthetic(name=f"campaign_{level}_s{seed}", coordinated=True)
    span = wd.end - wd.start
    link = f"https://synth-news-{seed}.example/story"
    promoted = [f"at://did:plc:synthtarget{seed:04d}/app.bsky.feed.post/p{k}" for k in range(4)]
    if level == "L1":
        t0 = wd.start + rng.uniform(0.2, 0.7) * span
        times = t0 + rng.uniform(0, 30, n_accounts)
        like_times = lambda i, k: t0 + 40 + rng.uniform(0, 50)  # noqa: E731
    elif level == "L2":
        t0 = wd.start + rng.uniform(0.1, 0.5) * span
        times = t0 + rng.uniform(0, 300, n_accounts)
        like_times = lambda i, k: t0 + rng.uniform(0, 240)  # noqa: E731
    elif level == "L3":
        times = wd.start + rng.uniform(0, span, n_accounts)
        like_times = lambda i, k: wd.start + rng.uniform(0, span)  # noqa: E731
    else:
        raise ValueError(level)
    for i in range(n_accounts):
        did = _did(level.lower(), i, seed)
        word = _WORDS[(seed + i) % len(_WORDS)]
        if level == "L1":
            info = _info(
                did,
                rng,
                (1, 10),
                (0, 4),
                handle=f"{word}{rng.integers(1000, 9999)}.bsky.social",
                avatar=False,
                desc=0,
                pds="synthpds.example",
            )
            text = " ".join(opts[0] for opts in _TEMPLATE)
        elif level == "L2":
            info = _info(
                did,
                rng,
                (20, 60),
                (2, 40),
                handle=f"{word}{rng.integers(10, 99)}.bsky.social",
                avatar=bool(rng.random() < 0.5),
                desc=int(rng.integers(0, 40)),
                pds="morel.us-east.host.bsky.network",
            )
            words = [opts[0] for opts in _TEMPLATE]
            for slot in rng.choice(len(_TEMPLATE), size=2, replace=False):
                words[slot] = _TEMPLATE[slot][int(rng.integers(0, 3))]
            text = " ".join(words)
        else:
            info = _info(
                did,
                rng,
                (200, 900),
                (60, 900),
                handle=f"{word}{'xyz'[i % 3]}{['writes', 'daily', 'official'][i % 3]}.bsky.social",
                avatar=True,
                desc=int(rng.integers(40, 200)),
                pds=f"pds{i % 4}.us-west.host.bsky.network",
            )
            text = _L3_SENTENCES[i % len(_L3_SENTENCES)]
        s.accounts[did] = info
        uri = _post_uri(did, i)
        s.posts.append(
            WPost(
                uri=uri,
                did=did,
                t=float(times[i]),
                text=text,
                text_hash=text_hash(text),
                embed_urls=[link],
                langs=["en"],
            )
        )
        for k, subj in enumerate(promoted):
            t = float(np.clip(like_times(i, k), wd.start, wd.end - 1))
            s.actions.append(
                WAction(
                    "like",
                    f"at://{did}/app.bsky.feed.like/synth{k}",
                    did,
                    t,
                    subj,
                    subj.split("/")[2],
                )
            )
        if level == "L3":  # unrelated organic-looking likes
            for k in range(6):
                subj = f"at://did:plc:synthrandom{rng.integers(1e9)}/app.bsky.feed.post/r{k}"
                s.actions.append(
                    WAction(
                        "like",
                        f"at://{did}/app.bsky.feed.like/synthr{k}",
                        did,
                        float(wd.start + rng.uniform(0, span)),
                        subj,
                        subj.split("/")[2],
                    )
                )
    return s


_FIRST = [
    "alex",
    "samira",
    "kimberly",
    "jo",
    "leena",
    "maxwell",
    "oskari",
    "priya",
    "tom",
    "ines",
    "yusuf",
    "marta",
    "chen",
    "ola",
    "ravi",
    "zoe",
]
_LAST = ["martin", "berg", "nakamura", "okafor", "virtanen", "silva", "novak", "kaur"]
_DOMAINS = ["bsky.social", "bsky.social", "bsky.social", "example.fi", "example.org"]


def _established(tag: str, i: int, seed: int, rng) -> tuple[str, AccountInfo]:
    """Old, followed accounts with varied, human-looking handles on varied hosts."""
    did = _did(tag, i, seed)
    first, last = _FIRST[int(rng.integers(len(_FIRST)))], _LAST[int(rng.integers(len(_LAST)))]
    style = int(rng.integers(4))
    local = [first, f"{first}{last}", f"{first}-{last}", f"{last}{first[0]}"][style]
    domain = _DOMAINS[int(rng.integers(len(_DOMAINS)))]
    handle = f"{local}.{domain}" if domain == "bsky.social" else f"{first}.{last}{i}.{domain}"
    return did, _info(
        did,
        rng,
        (380, 1000),
        (120, 5000),
        handle=handle,
        avatar=True,
        desc=int(rng.integers(20, 250)),
        pds=f"pds{i % 7}.host.bsky.network",
    )


def hard_negative(kind: str, wd: WindowData, seed: int = 0) -> Synthetic:
    rng = np.random.default_rng(10_000 + seed)
    s = Synthetic(name=f"negative_{kind}_s{seed}", coordinated=False)
    span = wd.end - wd.start
    if kind == "viral":
        subj = f"at://did:plc:synthstar{seed:04d}/app.bsky.feed.post/viral"
        t0 = wd.start + 0.1 * span
        for i in range(5000):
            did, info = _established("vir", i, seed, rng)
            s.accounts[did] = info
            t = float(min(wd.end - 1, t0 + rng.exponential(span / 4)))
            s.actions.append(
                WAction(
                    "like", f"at://{did}/app.bsky.feed.like/v", did, t, subj, subj.split("/")[2]
                )
            )
    elif kind == "news_link":
        link = f"https://synth-bignews-{seed}.example/2026/11/13/story"
        for i in range(60):
            did, info = _established("news", i, seed, rng)
            s.accounts[did] = info
            text = (
                f"{rng.choice(['Wow.', 'Read this.', 'Big if true:', 'Ugh.', 'Finally!'])} "
                f"{rng.choice(_L3_SENTENCES)} {rng.integers(1e6)}"
            )
            s.posts.append(
                WPost(
                    uri=_post_uri(did, i),
                    did=did,
                    t=float(wd.start + rng.uniform(0, span)),
                    text=text,
                    text_hash=text_hash(text),
                    embed_urls=[link],
                    langs=["en"],
                )
            )
    elif kind == "live_event":
        t0 = wd.start + 0.4 * span
        for i in range(40):
            did, info = _established("live", i, seed, rng)
            s.accounts[did] = info
            text = f"{rng.choice(_ORGANIC_SHORT)} #synthfinal"
            s.posts.append(
                WPost(
                    uri=_post_uri(did, i),
                    did=did,
                    t=float(t0 + rng.uniform(0, 120)),
                    text=text,
                    text_hash=text_hash(text),
                    langs=["en"],
                )
            )
    elif kind == "fandom":
        subjects = [f"at://did:plc:synthartist{seed:04d}/app.bsky.feed.post/a{k}" for k in range(5)]
        for i in range(30):
            did, info = _established("fan", i, seed, rng)
            s.accounts[did] = info
            for k, subj in enumerate(subjects):
                if rng.random() < 0.8:
                    s.actions.append(
                        WAction(
                            "like",
                            f"at://{did}/app.bsky.feed.like/f{k}",
                            did,
                            float(wd.start + rng.uniform(0, span)),
                            subj,
                            subj.split("/")[2],
                        )
                    )
    else:
        raise ValueError(kind)
    return s


def inject(wd: WindowData, *synths: Synthetic) -> WindowData:
    """A new WindowData with the synthetic events added (background counts updated too)."""
    out = WindowData(
        start=wd.start,
        end=wd.end,
        posts=list(wd.posts),
        actions=list(wd.actions),
        background={k: v.copy() for k, v in wd.background.items()},
        active_accounts=wd.active_accounts,
    )
    n = max(1, int(round(wd.end - wd.start)))
    for s in synths:
        out.posts.extend(s.posts)
        out.actions.extend(s.actions)
        for kind, times in (
            ("post", [p.t for p in s.posts]),
            *((k, [a.t for a in s.actions if a.kind == k]) for k in ("like", "repost", "follow")),
        ):
            if not times:
                continue
            bg = out.background.setdefault(kind, np.zeros(n))
            idx = np.clip((np.asarray(times) - wd.start).astype(int), 0, n - 1)
            np.add.at(bg, idx, 1)
        out.active_accounts += len(s.accounts)
    return out


def empty_window(
    start: float = 1_800_000_000.0,
    length: float = 600.0,
    seed: int = 0,
    organic_accounts: int = 400,
) -> WindowData:
    """A plain organic background: independent accounts posting unique texts and liking random
    posts at random times. Used by tests when no recorded slice is at hand."""
    rng = np.random.default_rng(seed)
    wd = WindowData(start=start, end=start + length)
    for i in range(organic_accounts):
        did = f"did:plc:bgacct{seed:03d}{i:06d}"
        for j in range(int(rng.integers(1, 4))):
            text = f"organic thought number {i} {j} about {rng.choice(_WORDS)} {rng.integers(1e9)}"
            wd.posts.append(
                WPost(
                    uri=_post_uri(did, j),
                    did=did,
                    t=float(start + rng.uniform(0, length)),
                    text=text,
                    text_hash=text_hash(text),
                    langs=["en"],
                )
            )
        for j in range(int(rng.integers(0, 6))):
            subj = f"at://did:plc:bgauthor{int(rng.integers(0, 3000))}/app.bsky.feed.post/x"
            wd.actions.append(
                WAction(
                    "like",
                    f"at://{did}/app.bsky.feed.like/b{j}",
                    did,
                    float(start + rng.uniform(0, length)),
                    subj,
                    subj.split("/")[2],
                )
            )
    times = {"post": [p.t for p in wd.posts], "like": [a.t for a in wd.actions]}
    wd.background = WindowData.background_from_times(wd.start, wd.end, times)
    wd.active_accounts = organic_accounts
    return wd
