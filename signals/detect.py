"""Detector entry point: window data -> scored clusters. Pure: no DB writes, deterministic.

    from signals.detect import detect
    results = detect(window_data, account_info=lambda dids: {...}, cfg=DEFAULT)

`account_info` is called once with all candidate members and returns {did: AccountInfo} for the
ones it knows (identity family and organic guards need it; pass None to skip them).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from signals.config import DEFAULT, DetectorConfig
from signals.graph import build_graph, communities
from signals.keys import MinHashCache, build_keys
from signals.score import AccountInfo, ClusterScore, score_cluster
from signals.window import WindowData

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
) -> DetectResult:
    t0 = time.monotonic()
    keys = build_keys(wd, cfg, mh_cache)
    t1 = time.monotonic()
    graph = build_graph(keys, cfg)
    groups = communities(graph, cfg)
    t2 = time.monotonic()
    # Small clusters first: they're the likely campaigns, and the enrichment budget is limited.
    ordered = list(dict.fromkeys(d for g in sorted(groups, key=len) for d in g))
    info = account_info(ordered) if account_info and groups else {}
    t3 = time.monotonic()
    clusters = [score_cluster(g, keys, wd, info, cfg) for g in groups]
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
        "seconds": {
            "keys": round(t1 - t0, 2),
            "graph": round(t2 - t1, 2),
            "account_info": round(t3 - t2, 2),
            "score": round(t4 - t3, 2),
        },
    }
    return DetectResult(clusters, stats)
