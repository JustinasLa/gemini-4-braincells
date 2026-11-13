"""Keys: things several accounts can share in a window. Each key belongs to one signal family.

content:    text (same normalised text), neardup (MinHash-similar text), media (same blob CID),
            url (same normalised link)
engagement: like / repost (same subject post), quote (same quoted post), follow (same followed DID)
"""

from collections import defaultdict
from dataclasses import dataclass, field

from datasketch import MinHash, MinHashLSH

from signals.config import DetectorConfig
from signals.textnorm import normalize_text
from signals.window import WindowData

CONTENT_KINDS = ("text", "neardup", "media", "url")
ENGAGEMENT_KINDS = ("like", "repost", "quote", "follow")
# Which background (per-second event counts) each key kind's actions come from.
BACKGROUND_KIND = {
    "text": "post",
    "neardup": "post",
    "media": "post",
    "url": "post",
    "quote": "post",
    "like": "like",
    "repost": "repost",
    "follow": "follow",
}


@dataclass(slots=True)
class Use:
    did: str
    t: float
    ref: str  # URI of the post/like/repost/follow record that used the key


@dataclass
class Key:
    kind: str
    value: str
    uses: list[Use] = field(default_factory=list)
    accounts: set[str] = field(default_factory=set)

    @property
    def family(self) -> str:
        return "content" if self.kind in CONTENT_KINDS else "engagement"

    @property
    def n(self) -> int:
        return len(self.accounts)


class MinHashCache:
    """MinHash signatures by post URI; windows overlap, so most posts are hashed once."""

    def __init__(self, num_perm: int, capacity: int = 300_000) -> None:
        self.num_perm = num_perm
        self.capacity = capacity
        self._d: dict[str, MinHash] = {}

    def get(self, uri: str, toks: list[str]) -> MinHash:
        mh = self._d.get(uri)
        if mh is None:
            mh = MinHash(num_perm=self.num_perm, seed=1)
            shingles = {" ".join(toks[i : i + 3]) for i in range(max(1, len(toks) - 2))}
            mh.update_batch([s.encode() for s in sorted(shingles)])
            if len(self._d) >= self.capacity:
                self._d.clear()
            self._d[uri] = mh
        return mh


def _text_ok(norm: str, cfg: DetectorConfig) -> bool:
    return len(norm) >= cfg.min_text_chars and len(norm.split()) >= cfg.min_text_tokens


def build_keys(
    wd: WindowData,
    cfg: DetectorConfig,
    mh_cache: MinHashCache | None = None,
) -> dict[tuple[str, str], Key]:
    keys: dict[tuple[str, str], Key] = {}

    def add(kind: str, value: str, did: str, t: float, ref: str) -> None:
        k = keys.get((kind, value))
        if k is None:
            k = keys[(kind, value)] = Key(kind, value)
        k.uses.append(Use(did, t, ref))
        k.accounts.add(did)

    long_posts: list[tuple[str, list[str], str, float]] = []
    for p in sorted(wd.posts, key=lambda p: (p.t, p.uri)):
        norm = normalize_text(p.text)
        if p.text_hash and _text_ok(norm, cfg):
            add("text", p.text_hash, p.did, p.t, p.uri)
            toks = norm.split()
            if len(toks) >= cfg.minhash_min_tokens:
                long_posts.append((p.uri, toks, p.did, p.t))
        for m in p.media_hashes:
            add("media", m, p.did, p.t, p.uri)
        for u in p.embed_urls:
            add("url", u, p.did, p.t, p.uri)
        if p.quote_uri:
            add("quote", p.quote_uri, p.did, p.t, p.uri)
    for a in sorted(wd.actions, key=lambda a: (a.t, a.uri)):
        add(a.kind, a.subject, a.did, a.t, a.uri)

    if long_posts:
        _near_duplicates(long_posts, cfg, mh_cache, add)

    return {kv: k for kv, k in keys.items() if cfg.min_key_accounts <= k.n <= cfg.max_key_accounts}


def _near_duplicates(long_posts, cfg: DetectorConfig, mh_cache, add) -> None:
    """Group near-duplicate texts (MinHash LSH + union-find) into 'neardup' keys."""
    cache = mh_cache or MinHashCache(cfg.minhash_perm)
    lsh = MinHashLSH(threshold=cfg.minhash_threshold, num_perm=cfg.minhash_perm)
    sigs = {}
    for uri, toks, _did, _t in long_posts:
        if uri in sigs:
            continue
        sigs[uri] = cache.get(uri, toks)
        lsh.insert(uri, sigs[uri])
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for uri in sigs:
        for other in lsh.query(sigs[uri]):
            if other != uri and sigs[uri].jaccard(sigs[other]) >= cfg.minhash_threshold:
                a, b = find(uri), find(other)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    groups: dict[str, list] = defaultdict(list)
    for uri, _toks, did, t in long_posts:
        groups[find(uri)].append((uri, did, t))
    for root, members in groups.items():
        if len({d for _, d, _ in members}) >= 2:
            for uri, did, t in members:
                add("neardup", root, did, t, uri)
