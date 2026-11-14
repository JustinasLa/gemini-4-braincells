"""Account-account co-action graph and community detection.

Edge weight between two accounts = sum over keys they share of a rarity weight 1/ln(1 + n),
where n is the number of distinct accounts using the key in the window: a key shared by 3
accounts weighs 0.72, one shared by 150 weighs 0.20, and keys above max_key_accounts (viral posts,
big news links) are dropped before this point. An edge exists if the pair shares at least one
content key, or at least `min_engagement_shared` distinct engagement keys (one co-like is noise;
repeated co-likes are behaviour).
"""

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from signals.config import DetectorConfig
from signals.keys import Key


@dataclass
class Graph:
    dids: list[str]
    edges: list[tuple[int, int, float, int, int]]  # i, j, weight, content_shared, engagement_shared


def rarity(n: int) -> float:
    return 1.0 / float(np.log1p(n))


def build_graph(keys: dict[tuple[str, str], Key], cfg: DetectorConfig) -> Graph:
    dids = sorted({d for k in keys.values() for d in k.accounts})
    index = {d: i for i, d in enumerate(dids)}
    mats = {}
    for family in ("content", "engagement"):
        fam_keys = sorted((kv for kv, k in keys.items() if k.family == family))
        rows, cols, wts = [], [], []
        for j, kv in enumerate(fam_keys):
            k = keys[kv]
            w = np.sqrt(rarity(k.n))
            for d in sorted(k.accounts):
                rows.append(index[d])
                cols.append(j)
                wts.append(w)
        shape = (len(dids), max(1, len(fam_keys)))
        b = sparse.csr_matrix((wts, (rows, cols)), shape=shape)
        bb = sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=shape)
        mats[family] = (
            sparse.triu(b @ b.T, k=1).tocsr(),  # weighted
            sparse.triu(bb @ bb.T, k=1).tocsr(),  # counts
        )
    cw, cn = mats["content"]
    ew, en = mats["engagement"]
    en_ok = en.multiply(en >= cfg.min_engagement_shared)
    ew_ok = ew.multiply(en >= cfg.min_engagement_shared)
    weight = (cw + ew_ok).tocoo()
    # Vectorised lookups of the shared-key counts for every weighted pair (was a dict lookup per
    # pair: ~15 s on an hour of data).
    r, c_, w = weight.row, weight.col, weight.data
    pos = w > 0
    r, c_, w = r[pos], c_[pos], w[pos]
    if len(r):
        cnt_c = np.asarray(cn.tocsr()[r, c_]).ravel().astype(int)
        cnt_e = np.asarray(en_ok.tocsr()[r, c_]).ravel().astype(int)
    else:
        cnt_c = cnt_e = np.zeros(0, dtype=int)
    keep = (cnt_c >= 1) | (cnt_e >= cfg.min_engagement_shared)
    r, c_, w, cnt_c, cnt_e = r[keep], c_[keep], w[keep], cnt_c[keep], cnt_e[keep]
    order = np.lexsort((c_, r))  # sorted by (i, j), as before
    edges = [
        (int(r[k]), int(c_[k]), float(w[k]), int(cnt_c[k]), int(cnt_e[k])) for k in order.tolist()
    ]
    return Graph(dids=dids, edges=edges)


def communities(g: Graph, cfg: DetectorConfig) -> list[list[str]]:
    """Connected components; components bigger than leiden_above are split with Leiden
    (seeded, so deterministic). Returns member DID lists, each sorted, size >= min_cluster_size."""
    import igraph as ig

    if not g.edges:
        return []
    graph = ig.Graph(n=len(g.dids), edges=[(i, j) for i, j, *_ in g.edges], directed=False)
    graph.es["weight"] = [w for _, _, w, *_ in g.edges]
    out: list[list[str]] = []
    for comp in graph.connected_components():
        if len(comp) < cfg.min_cluster_size:
            continue
        if len(comp) <= cfg.leiden_above:
            out.append(sorted(g.dids[v] for v in comp))
            continue
        sub = graph.subgraph(comp)
        parts = _leiden(sub, cfg)
        for part in parts:
            if cfg.min_cluster_size <= len(part) <= cfg.max_cluster_size:
                out.append(sorted(g.dids[comp[v]] for v in part))
    out.sort(key=lambda m: (-len(m), m[0]))
    return out


def _leiden(sub, cfg: DetectorConfig) -> list[list[int]]:
    import leidenalg

    part = leidenalg.find_partition(
        sub,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=cfg.leiden_resolution,
        seed=cfg.seed,
    )
    return [list(c) for c in part]
