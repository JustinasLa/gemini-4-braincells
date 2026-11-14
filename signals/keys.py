"""Keys: things several accounts can share in a window. Each key belongs to one signal family.

content:    text (same normalised text), neardup (MinHash-similar text), media (same blob CID),
            url (same normalised link)
engagement: like / repost (same subject post), quote (same quoted post), follow (same followed DID)
"""

from collections import defaultdict
from dataclasses import dataclass, field

from datasketch import MinHash, MinHashLSH

from signals.config import DetectorConfig
from signals.textnorm import is_unspaced, normalize_text, shingles, word_count
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
    """Per-post work that consecutive (overlapping) windows would otherwise redo: normalised
    text, MinHash signatures, and an incrementally maintained LSH index with the near-duplicate
    pairs found so far. A fresh cache gives exactly the same result as a warm one."""

    def __init__(self, num_perm: int, capacity: int = 300_000) -> None:
        self.num_perm = num_perm
        self.capacity = capacity
        self._d: dict[str, MinHash] = {}
        self._norm: dict[str, str] = {}
        self._lsh: MinHashLSH | None = None
        self._lsh_threshold: float | None = None
        self._lsh_sigs: dict[str, MinHash] = {}
        self._adj: dict[str, set[str]] = {}
        # Permutations depend only on (num_perm, seed): compute them once, not per post.
        template = MinHash(num_perm=num_perm, seed=1)
        self._perms = template.permutations
        self._scheme = template.scheme

    def norm(self, uri: str, text: str | None) -> str:
        n = self._norm.get(uri)
        if n is None:
            n = normalize_text(text)
            if len(self._norm) >= self.capacity:
                self._norm.clear()
            self._norm[uri] = n
        return n

    def near_duplicate_pairs(
        self, items: list[tuple[str, list[str]]], threshold: float
    ) -> dict[str, set[str]]:
        """Adjacency of near-duplicate posts (MinHash Jaccard >= threshold) among `items`.
        Posts that left the window are removed from the index, new ones are queried against
        everything still in it and then inserted: every pair is checked exactly once, as in a
        from-scratch build, because sharing an LSH band is symmetric."""
        if self._lsh is None or self._lsh_threshold != threshold:
            self._lsh = MinHashLSH(threshold=threshold, num_perm=self.num_perm)
            self._lsh_threshold = threshold
            self._lsh_sigs, self._adj = {}, {}
        current = {u for u, _ in items}
        for u in [u for u in self._lsh_sigs if u not in current]:
            self._lsh.remove(u)
            del self._lsh_sigs[u]
            for n in self._adj.pop(u, ()):
                self._adj[n].discard(u)
        for u, toks in items:
            if u in self._lsh_sigs:
                continue
            sig = self.get(u, toks)
            nbrs = {o for o in self._lsh.query(sig) if self._lsh_sigs[o].jaccard(sig) >= threshold}
            self._lsh.insert(u, sig)
            self._lsh_sigs[u] = sig
            self._adj[u] = nbrs
            for o in nbrs:
                self._adj[o].add(u)
        return self._adj

    def get(self, uri: str, shingle_set: set[str]) -> MinHash:
        mh = self._d.get(uri)
        if mh is None:
            mh = MinHash(
                num_perm=self.num_perm, seed=1, permutations=self._perms, scheme=self._scheme
            )
            mh.update_batch([s.encode() for s in sorted(shingle_set)])
            if len(self._d) >= self.capacity:
                self._d.clear()
            self._d[uri] = mh
        return mh


def _text_ok(norm: str, cfg: DetectorConfig) -> bool:
    """Long enough to be a meaningful shared text. Unspaced scripts (Japanese, Chinese, Thai...)
    pack more meaning per character, so they need half the characters."""
    chars_ok = len(norm) >= cfg.min_text_chars or (
        is_unspaced(norm) and len(norm.replace(" ", "")) >= cfg.min_text_chars // 2
    )
    return chars_ok and word_count(norm) >= cfg.min_text_tokens


def build_keys(
    wd: WindowData,
    cfg: DetectorConfig,
    mh_cache: MinHashCache | None = None,
) -> dict[tuple[str, str], Key]:
    keys: dict[tuple[str, str], Key] = {}
    cache = mh_cache or MinHashCache(cfg.minhash_perm)

    def add(kind: str, value: str, did: str, t: float, ref: str) -> None:
        k = keys.get((kind, value))
        if k is None:
            k = keys[(kind, value)] = Key(kind, value)
        k.uses.append(Use(did, t, ref))
        k.accounts.add(did)

    long_posts: list[tuple[str, list[str], str, float]] = []
    for p in sorted(wd.posts, key=lambda p: (p.t, p.uri)):
        norm = cache.norm(p.uri, p.text)
        if p.text_hash and _text_ok(norm, cfg):
            add("text", p.text_hash, p.did, p.t, p.uri)
            if word_count(norm) >= cfg.minhash_min_tokens:
                long_posts.append((p.uri, shingles(norm), p.did, p.t))
        for m in p.media_hashes:
            add("media", m, p.did, p.t, p.uri)
        for u in p.embed_urls:
            add("url", u, p.did, p.t, p.uri)
        if p.quote_uri:
            add("quote", p.quote_uri, p.did, p.t, p.uri)
    for a in sorted(wd.actions, key=lambda a: (a.t, a.uri)):
        add(a.kind, a.subject, a.did, a.t, a.uri)

    if long_posts:
        _near_duplicates(long_posts, cfg, cache, add)

    return {kv: k for kv, k in keys.items() if cfg.min_key_accounts <= k.n <= cfg.max_key_accounts}


def _near_duplicates(long_posts, cfg: DetectorConfig, cache: MinHashCache, add) -> None:
    """Group near-duplicate texts (MinHash LSH + union-find) into 'neardup' keys. A group's key
    is its smallest post URI (union by smaller root), so it doesn't depend on merge order."""
    seen: dict[str, list[str]] = {}
    for uri, toks, _did, _t in long_posts:
        seen.setdefault(uri, toks)
    adj = cache.near_duplicate_pairs(list(seen.items()), cfg.minhash_threshold)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for uri in seen:
        for other in adj.get(uri, ()):
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
