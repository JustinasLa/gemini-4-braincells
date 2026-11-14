"""Detector entry point: window data -> scored clusters. Pure: no DB writes, deterministic.

    from signals.detect import detect
    results = detect(window_data, account_info=lambda dids: {...}, cfg=DEFAULT)

`account_info` is called once with all candidate members and returns {did: AccountInfo} for the
ones it knows (identity family and organic guards need it; pass None to skip them).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from signals.baseline import BaseRates
from signals.config import DEFAULT, DetectorConfig
from signals.graph import build_graph, communities
from signals.keys import MinHashCache, build_keys
from signals.score import (
    AccountInfo,
    ClusterScore,
    key_index,
    score_cluster,
    semantic_candidate_posts,
    semantic_signal,
)
from signals.semantic import EmbedFn
from signals.textnorm import normalize_text
from signals.window import WindowData


def _long_posters(members: list[str], wd: WindowData, cfg: DetectorConfig) -> int:
    mset = set(members)
    return len(
        {
            p.did
            for p in wd.posts
            if p.did in mset and len(normalize_text(p.text).split()) >= cfg.semantic_min_tokens
        }
    )


AccountInfoFn = Callable[[list[str]], dict[str, AccountInfo]]


@dataclass
class DetectResult:
    clusters: list[ClusterScore]
    stats: dict = field(default_factory=dict)


def detect(
    wd: WindowData,
    account_info: AccountInfoFn | None = None,
    cfg: DetectorConfig = DEFAULT,
    mh_cache: MinHashCache | None = None,
    embed: EmbedFn | None = None,
    base_rates: BaseRates | None = None,
) -> DetectResult:
    t0 = time.monotonic()
    keys = build_keys(wd, cfg, mh_cache)
    t1 = time.monotonic()
    graph = build_graph(keys, cfg)
    groups = communities(graph, cfg)
    t2 = time.monotonic()
    # Pass 1 without account info. Then fetch account info only for clusters where something
    # looked suspicious (strongest first: the enrichment budget is limited) and rescore those.
    idx = key_index(keys)
    clusters = [score_cluster(g, keys, wd, {}, cfg, index=idx) for g in groups]
    suspicious = sorted(
        (i for i, c in enumerate(clusters) if max(c.family_scores.values()) >= cfg.family_fire),
        key=lambda i: (-clusters[i].score, len(clusters[i].members), clusters[i].members[0]),
    )
    ordered = list(dict.fromkeys(d for i in suspicious for d in clusters[i].members))
    info = account_info(ordered) if account_info and ordered else {}
    t3 = time.monotonic()
    # Rescore with account info, and with Gemini embeddings (paraphrase detection) when given:
    # for the suspicious clusters plus small clusters with enough long posts to compare.
    rescore = set(suspicious)
    if embed is not None:
        rescore |= {
            i
            for i, g in enumerate(groups)
            if len(g) <= cfg.semantic_max_cluster
            and _long_posters(g, wd, cfg) >= cfg.min_cluster_size
        }
    if embed is not None and rescore:
        # One batched prefetch for all candidates; per-cluster calls below then hit the cache.
        posts = semantic_candidate_posts([groups[i] for i in sorted(rescore)], wd, cfg)
        if posts:
            embed([p.uri for p in posts], [p.text or "" for p in posts])
    sus = set(suspicious)
    for i in sorted(rescore):
        if i not in sus:
            # Not suspicious yet: only a full rescore if the (cheap, cached) semantic check fires.
            sem = semantic_signal(groups[i], wd, embed, cfg)
            if sem is None or sem.score < cfg.family_fire:
                continue
        if embed is not None or any(d in info for d in clusters[i].members):
            clusters[i] = score_cluster(
                groups[i], keys, wd, info, cfg, embed, base_rates, index=idx
            )
    t4 = time.monotonic()
    by_status: dict[str, int] = {}
    for c in clusters:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    stats = {
        "posts": len(wd.posts),
        "actions": len(wd.actions),
        "keys": len(keys),
        "graph_accounts": len(graph.dids),
        "edges": len(graph.edges),
        "clusters": len(clusters),
        "by_status": by_status,
        "accounts_with_info": len(info),
        "base_rates": base_rates.as_dict() if base_rates else None,
        "rescored": len(rescore),
        "seconds": {
            "keys": round(t1 - t0, 2),
            "graph": round(t2 - t1, 2),
            "score_and_enrich": round(t3 - t2, 2),
            "rescore": round(t4 - t3, 2),
        },
    }
    return DetectResult(clusters, stats)
