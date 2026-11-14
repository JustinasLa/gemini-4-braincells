"""Semantic duplicates with Gemini embeddings: accounts saying the same thing in different words.

Calibrated 2026-11-13 on Gemini-written fictional campaigns vs organic look-alikes vs live posts
(gemini-embedding-001, task SEMANTIC_SIMILARITY, 768 dims): at cosine >= 0.85, 67% of
same-campaign post pairs pass, 5% of same-topic organic pairs and 0.04% of random live pairs.
So this says "same message", not "coordinated": it is one content signal, and the status still
needs a second, behavioural family (timing, network or identity).

Cost control: only posts of members of candidate clusters (already suspicious, or small with
enough long posts) are embedded, each post once (cached by URI), batched 100 per request, at
most `budget_per_call` new posts per call.
"""

import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

MODEL = os.environ.get("SEMANTIC_MODEL", "gemini-embedding-001")
DIMS = 768

# (uris, texts) -> unit vectors (len(uris), d); rows of NaN for posts that couldn't be embedded.
EmbedFn = Callable[[list[str], list[str]], np.ndarray]


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = MODEL,
        capacity: int = 200_000,
        budget_per_call: int = 2000,
    ) -> None:
        from google import genai

        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])
        self.model = model
        self.cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self.capacity = capacity
        self.budget_per_call = budget_per_call
        self.calls = 0
        self.embedded = 0

    def __call__(self, uris: list[str], texts: list[str]) -> np.ndarray:
        from google.genai import types

        todo = [(u, t) for u, t in zip(uris, texts, strict=True) if u not in self.cache]
        todo = todo[: self.budget_per_call]
        cfg = types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=DIMS)
        chunks = [todo[k : k + 100] for k in range(0, len(todo), 100)]

        def call(chunk):
            t0 = time.monotonic()
            try:
                res = self.client.models.embed_content(
                    model=self.model, contents=[t[:2000] for _, t in chunk], config=cfg
                )
            except Exception as exc:  # embeddings are optional evidence: never break detection
                log.warning("embedding call failed: %s: %s", type(exc).__name__, exc)
                return chunk, None
            log.info(
                "gemini embed model=%s n=%d latency_ms=%d",
                self.model,
                len(chunk),
                (time.monotonic() - t0) * 1000,
            )
            return chunk, res

        with ThreadPoolExecutor(max_workers=4) as pool:
            for chunk, res in pool.map(call, chunks):
                if res is None:
                    continue
                self.calls += 1
                self.embedded += len(chunk)
                for (u, _), e in zip(chunk, res.embeddings, strict=True):
                    v = np.asarray(e.values, dtype=np.float32)
                    self.cache[u] = v / (np.linalg.norm(v) or 1.0)
                    if len(self.cache) > self.capacity:
                        self.cache.popitem(last=False)
        out = np.full((len(uris), DIMS), np.nan, dtype=np.float32)
        for i, u in enumerate(uris):
            if u in self.cache:
                out[i] = self.cache[u]
        return out


def semantic_core(
    dids: list[str], vecs: np.ndarray, threshold: float, min_share: float = 1 / 3
) -> tuple[set[str], float]:
    """Accounts whose posts say the same thing as at least `min_share` of the OTHER accounts
    (cosine >= threshold between any of their posts), and the cross-account pair density.

    Density, not chaining: in a same-topic organic conversation ~5% of cross-account pairs pass
    the threshold, so a chain A~B~C can link many people while each is similar to only one or two
    others. In a paraphrase campaign most pairs pass, so most accounts are in the core."""
    ok = ~np.isnan(vecs).any(axis=1)
    idx = np.flatnonzero(ok)
    accounts = sorted({dids[i] for i in idx})
    if len(accounts) < 2:
        return set(), 0.0
    pos = {d: k for k, d in enumerate(accounts)}
    sim = vecs[idx] @ vecs[idx].T >= threshold
    owner = np.array([pos[dids[i]] for i in idx])
    n = len(accounts)
    link = np.zeros((n, n), dtype=bool)
    ii, jj = np.nonzero(sim)
    a, b = owner[ii], owner[jj]
    keep = a != b
    link[a[keep], b[keep]] = True
    link |= link.T
    density = float(link[np.triu_indices(n, 1)].mean())
    need = max(1, int(np.ceil(min_share * (n - 1))))
    core = {accounts[k] for k in range(n) if link[k].sum() >= need}
    return core, density
