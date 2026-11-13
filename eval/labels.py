"""Hand labels on live clusters -> live precision, false-positive rate and labeller agreement.

    python -m eval labels sample --name live-v1 --n 100  # freeze a stratified sample (reads DB)
    python -m eval labels serve --port 8000              # labelling page (eval/label_server.py)
    python -m eval labels score --name live-v1           # print; + --database-url URL to write

Files live outside git under LABELS_DIR (default /srv/junction-espoo/data/labels): the sample is
real Bluesky content, so it isn't committed. `samples/<name>.json` is the frozen sample
(write-once), `<name>.labels.jsonl` is append-only (latest record per labeller and item wins).

Blinding: each sample item has a `detector` block (status, score...) and an `evidence` block (what
the labeller sees). The labelling server only ever sends `evidence`, and items are numbered in a
shuffled order, so neither the status, the score nor the cluster id reaches the labeller.
"""

import argparse
import fcntl
import json
import logging
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.recall import _git_sha, wilson  # shared helpers (Wilson CI, eval_commit)

log = logging.getLogger("eval.labels")

DATASET = "live_hand_labels"
LABELS = ("coordinated", "organic", "cant_tell")
DEFINITE = ("coordinated", "organic")
STATUSES = ("coordinated", "likely_organic", "insufficient")
# Share of the sample each detector status gets before capping at what exists. Coordinated is
# oversampled (it is rare and its precision is the headline); weights undo this for FPR.
STATUS_SHARE = {"coordinated": 0.4, "likely_organic": 0.3, "insufficient": 0.3}
SAMPLE_RULE_VERSION = "labels-sample-v1"


def labels_dir() -> Path:
    return Path(os.environ.get("LABELS_DIR", "/srv/junction-espoo/data/labels"))


def sample_path(name: str, root: Path | None = None) -> Path:
    return (root or labels_dir()) / "samples" / f"{name}.json"


def labels_path(name: str, root: Path | None = None) -> Path:
    return (root or labels_dir()) / f"{name}.labels.jsonl"


def split_of(ts: datetime) -> str:
    """Shared split rule (eval/slices.py): odd UTC hours are heldout, even hours dev."""
    return "heldout" if ts.astimezone(UTC).hour % 2 else "dev"


# ---------------------------------------------------------------- sampling (pure part)


@dataclass
class Candidate:
    cluster_id: int
    campaign_id: int | None
    status: str
    score: float
    window_start: datetime


def one_per_campaign(cands: list[Candidate], rng: random.Random) -> list[Candidate]:
    """Episodes of one campaign (and the 10 min / 1 h windows over the same accounts) are near
    copies; labelling several would count one judgement many times. Keep one random episode."""
    groups: dict[object, list[Candidate]] = defaultdict(list)
    for c in sorted(cands, key=lambda c: c.cluster_id):
        groups[c.campaign_id if c.campaign_id is not None else ("solo", c.cluster_id)].append(c)
    return sorted((rng.choice(g) for g in groups.values()), key=lambda c: c.cluster_id)


def _largest_remainder(total: int, weights: dict, caps: dict) -> dict:
    """Split `total` over keys in proportion to `weights`, never above `caps`; what a capped key
    can't take goes to the others (same proportions)."""
    alloc = {k: 0 for k in weights}
    left = min(total, sum(caps.values()))
    while left > 0:
        open_ = {k: w for k, w in weights.items() if alloc[k] < caps[k] and w > 0}
        if not open_:
            break
        wsum = sum(open_.values())
        raw = {k: left * w / wsum for k, w in open_.items()}
        give = {k: min(int(math.floor(v)), caps[k] - alloc[k]) for k, v in raw.items()}
        rest = left - sum(give.values())
        for k in sorted(open_, key=lambda k: (-(raw[k] - math.floor(raw[k])), str(k))):
            if rest <= 0:
                break
            if alloc[k] + give[k] < caps[k]:
                give[k] += 1
                rest -= 1
        if sum(give.values()) == 0:
            break
        for k, v in give.items():
            alloc[k] += v
        left -= sum(give.values())
    return alloc


def score_bands(cands: list[Candidate], n_bands: int, rng: random.Random) -> list[list[Candidate]]:
    """Equal-count bands by score rank (ties broken at random), lowest score first."""
    keyed = sorted(cands, key=lambda c: (c.score, rng.random()))
    n_bands = max(1, min(n_bands, len(keyed)))
    return [
        keyed[len(keyed) * b // n_bands : len(keyed) * (b + 1) // n_bands] for b in range(n_bands)
    ]


def stratified_sample(
    cands: list[Candidate], n: int, seed: int, n_bands: int = 4
) -> tuple[list[dict], dict]:
    """Stratify by detector status x score band. Returns (picked, strata) where each picked entry
    is {candidate, stratum, weight} and weight = stratum population / stratum sample size."""
    rng = random.Random(seed)
    by_status = {s: [c for c in cands if c.status == s] for s in STATUSES}
    quota = _largest_remainder(n, STATUS_SHARE, {s: len(v) for s, v in by_status.items()})
    picked, strata = [], {}
    for status in STATUSES:
        bands = score_bands(by_status[status], n_bands, rng) if by_status[status] else []
        band_quota = _largest_remainder(
            quota[status],
            {b: 1 for b in range(len(bands))},
            {b: len(x) for b, x in enumerate(bands)},
        )
        for b, members in enumerate(bands):
            k = band_quota[b]
            key = f"{status}/b{b}"
            strata[key] = {
                "status": status,
                "band": b,
                "score_min": round(min(c.score for c in members), 4),
                "score_max": round(max(c.score for c in members), 4),
                "population": len(members),
                "sampled": k,
            }
            for c in rng.sample(members, k):
                picked.append({"candidate": c, "stratum": key, "weight": len(members) / k})
    rng.shuffle(picked)  # labelling order mixes strata
    return picked, strata


# ---------------------------------------------------------------- evidence (pure part)


def bsky_post_url(uri: str | None) -> str | None:
    parts = uri.removeprefix("at://").split("/") if uri and uri.startswith("at://") else []
    if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return None


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat(timespec="seconds") if t else None


def _age_days(created: datetime | None, at: datetime) -> float | None:
    return round((at - created).total_seconds() / 86400, 1) if created else None


def build_evidence(
    window_start: datetime,
    window_end: datetime,
    window_kind: str | None,
    members: list[dict],
    posts: list[dict],
    actions: list[dict],
    subject_texts: dict[str, dict],
    *,
    n_buckets: int = 30,
    max_posts: int = 30,
    max_members: int = 80,
) -> dict:
    """What a labeller sees: raw behaviour of the cluster's accounts in its window. Nothing here
    comes from the detector's output (no status, score, p-value, signal, core/peripheral role or
    analyst text). Media/source evidence is left out too: the analyst only runs its tools for
    some statuses, so its presence alone would reveal the status.

    members: {did, role, handle, display_name, created_at, did_created_at, followers, follows,
              posts_count, has_avatar, description_len, status}
    posts:   {uri, did, text, created_at, seen_at, reply_to, quote_uri, embed_urls, media_hashes,
              text_hash}
    actions: {kind like|repost|follow, did, subject_uri, subject_did, seen_at}
    """
    handle = {m["did"]: m.get("handle") or m["did"] for m in members}
    member_dids = set(handle)
    span = max((window_end - window_start).total_seconds(), 1.0)

    # timeline: every member action in the window, by kind
    tl = {k: [0] * n_buckets for k in ("post", "like", "repost", "follow")}
    events = [("post", p["seen_at"]) for p in posts] + [(a["kind"], a["seen_at"]) for a in actions]
    for kind, t in events:
        if t is None:
            continue
        b = int((t - window_start).total_seconds() / span * n_buckets)
        tl[kind][min(max(b, 0), n_buckets - 1)] += 1

    # per-member activity
    act = defaultdict(Counter)
    for p in posts:
        act[p["did"]]["post"] += 1
    for a in actions:
        act[a["did"]][a["kind"]] += 1

    # things several members engaged with (likes, reposts, replies, quotes)
    eng: dict[str, dict] = {}
    for a in actions:
        if a["kind"] in ("like", "repost") and a.get("subject_uri"):
            e = eng.setdefault(a["subject_uri"], {"kinds": Counter(), "dids": set(), "times": []})
            e["kinds"][a["kind"]] += 1
            e["dids"].add(a["did"])
            e["times"].append(a["seen_at"])
    for p in posts:
        for kind, uri in (("reply", p.get("reply_to")), ("quote", p.get("quote_uri"))):
            if uri:
                e = eng.setdefault(uri, {"kinds": Counter(), "dids": set(), "times": []})
                e["kinds"][kind] += 1
                e["dids"].add(p["did"])
                e["times"].append(p["seen_at"])
    targets = []
    for uri, e in eng.items():
        if len(e["dids"]) < 2:
            continue
        ts = sorted(t for t in e["times"] if t)
        subj = subject_texts.get(uri) or {}
        author = uri.removeprefix("at://").split("/")[0] if uri.startswith("at://") else None
        targets.append(
            {
                "url": bsky_post_url(uri),
                "uri": uri,
                "accounts": len(e["dids"]),
                "actions": dict(e["kinds"]),
                "spread_s": round((ts[-1] - ts[0]).total_seconds()) if len(ts) > 1 else 0,
                "author_is_member": author in member_dids,
                "text": (subj.get("text") or "")[:280] or None,
            }
        )
    targets.sort(key=lambda t: (-t["accounts"], -sum(t["actions"].values()), t["uri"]))

    follow_targets = Counter()
    follow_dids = defaultdict(set)
    for a in actions:
        if a["kind"] == "follow" and a.get("subject_did"):
            follow_dids[a["subject_did"]].add(a["did"])
    for d, s in follow_dids.items():
        if len(s) >= 2:
            follow_targets[d] = len(s)

    links = defaultdict(lambda: {"posts": 0, "dids": set()})
    for p in posts:
        for u in p.get("embed_urls") or []:
            links[u]["posts"] += 1
            links[u]["dids"].add(p["did"])
    shared_links = sorted(
        ({"url": u, "posts": v["posts"], "accounts": len(v["dids"])} for u, v in links.items()),
        key=lambda x: (-x["accounts"], -x["posts"], x["url"]),
    )

    texts = defaultdict(lambda: {"posts": 0, "dids": set(), "text": None})
    for p in posts:
        key = p.get("text_hash") or ((p.get("text") or "").strip().lower() or None)
        if key:
            texts[key]["posts"] += 1
            texts[key]["dids"].add(p["did"])
            texts[key]["text"] = texts[key]["text"] or p.get("text")
    repeated = sorted(
        (
            {"text": (v["text"] or "")[:280], "posts": v["posts"], "accounts": len(v["dids"])}
            for v in texts.values()
            if v["posts"] >= 2
        ),
        key=lambda x: (-x["accounts"], -x["posts"]),
    )

    ps = sorted(posts, key=lambda p: (p["seen_at"] or window_start, p["uri"]))
    if len(ps) > max_posts:  # evenly spaced over the window, so a long tail isn't cut off
        ps = [ps[i * len(ps) // max_posts] for i in range(max_posts)]
    post_rows = [
        {
            "url": bsky_post_url(p["uri"]),
            "handle": handle.get(p["did"], p["did"]),
            "text": (p.get("text") or "")[:500],
            "seen_at": _iso(p["seen_at"]),
            "t_s": round((p["seen_at"] - window_start).total_seconds()) if p["seen_at"] else None,
            "reply": bool(p.get("reply_to")),
            "quote": bool(p.get("quote_uri")),
            "links": list(p.get("embed_urls") or [])[:3],
            "media": len(p.get("media_hashes") or []),
        }
        for p in ps
    ]

    member_rows = []
    for m in members:
        created = m.get("did_created_at") or m.get("created_at")
        c = act.get(m["did"], Counter())
        member_rows.append(
            {
                "handle": m.get("handle") or m["did"],
                "display_name": m.get("display_name"),
                "profile_url": f"https://bsky.app/profile/{m['did']}",
                "age_days": _age_days(created, window_start),
                "followers": m.get("followers"),
                "follows": m.get("follows"),
                "posts_count": m.get("posts_count"),
                "has_avatar": m.get("has_avatar"),
                "has_bio": (m.get("description_len") or 0) > 0
                if m.get("description_len") is not None
                else None,
                "account_status": m.get("status"),
                "activity": dict(c),
            }
        )
    member_rows.sort(key=lambda r: (-sum(r["activity"].values()), r["handle"]))
    ages = [r["age_days"] for r in member_rows if r["age_days"] is not None]
    fols = [r["followers"] for r in member_rows if r["followers"] is not None]

    return {
        "window": {
            "start": _iso(window_start),
            "end": _iso(window_end),
            "minutes": round(span / 60, 1),
            "kind": window_kind,
        },
        "summary": {
            "accounts": len(member_rows),
            "with_age": len(ages),
            "median_age_days": round(statistics.median(ages), 1) if ages else None,
            "younger_than_30d": sum(a < 30 for a in ages),
            "median_followers": statistics.median(fols) if fols else None,
            "no_avatar": sum(r["has_avatar"] is False for r in member_rows),
            "posts": len(posts),
            "likes": sum(a["kind"] == "like" for a in actions),
            "reposts": sum(a["kind"] == "repost" for a in actions),
            "follows": sum(a["kind"] == "follow" for a in actions),
        },
        "timeline": {"buckets": n_buckets, "bucket_s": round(span / n_buckets, 1), **tl},
        "members": member_rows[:max_members],
        "members_hidden": max(0, len(member_rows) - max_members),
        "posts": post_rows,
        "shared_targets": targets[:15],
        "follow_targets": [
            {"profile_url": f"https://bsky.app/profile/{d}", "accounts": k}
            for d, k in follow_targets.most_common(10)
        ],
        "shared_links": shared_links[:15],
        "repeated_texts": repeated[:10],
    }


# ---------------------------------------------------------------- sampling (DB part)


def fetch_candidates(
    conn, settled_min: float, max_age_h: float, include_synthetic: bool = False
) -> list[Candidate]:
    """max_age_h: only windows that started within this many hours, so the raw likes/reposts/
    follows they need are still retained (ingest prunes likes after 12 h, the rest after 24 h);
    otherwise the frozen evidence would silently show zero activity."""
    rows = conn.execute(
        """
        SELECT id, campaign_id, status, score, window_start FROM clusters
        WHERE updated_at < now() - make_interval(secs => %s)
          AND window_start > now() - make_interval(secs => %s)
          AND (%s OR NOT coalesce(synthetic, false))
        ORDER BY id
        """,
        (settled_min * 60, max_age_h * 3600, include_synthetic),
    ).fetchall()
    return [Candidate(r[0], r[1], r[2], float(r[3]), r[4]) for r in rows]


def fetch_cluster(conn, cluster_id: int) -> tuple[dict, dict]:
    """(detector block, evidence block) for one cluster, read from the live tables."""
    c = conn.execute(
        """
        SELECT id, campaign_id, status, score, confidence, p_value, families_fired, size,
               window_kind, detector_version, window_start, window_end, updated_at
        FROM clusters WHERE id = %s
        """,
        (cluster_id,),
    ).fetchone()
    cols = [
        "cluster_id",
        "campaign_id",
        "status",
        "score",
        "confidence",
        "p_value",
        "families_fired",
        "size",
        "window_kind",
        "detector_version",
        "window_start",
        "window_end",
        "updated_at",
    ]
    det = dict(zip(cols, c, strict=True))
    ws, we = det["window_start"], det["window_end"]
    mcols = [
        "did",
        "role",
        "handle",
        "display_name",
        "created_at",
        "did_created_at",
        "followers",
        "follows",
        "posts_count",
        "has_avatar",
        "description_len",
        "status",
    ]
    members = [
        dict(zip(mcols, r, strict=True))
        for r in conn.execute(
            f"""
            SELECT m.did, m.role, {", ".join("a." + x for x in mcols[2:])}
            FROM cluster_members m LEFT JOIN accounts a ON a.did = m.did
            WHERE m.cluster_id = %s
            """,
            (cluster_id,),
        ).fetchall()
    ]
    dids = [m["did"] for m in members]
    pcols = [
        "uri",
        "did",
        "text",
        "created_at",
        "seen_at",
        "reply_to",
        "quote_uri",
        "embed_urls",
        "media_hashes",
        "text_hash",
    ]
    sel = ", ".join(pcols)
    posts = {
        r[0]: dict(zip(pcols, r, strict=True))
        for r in conn.execute(
            f"""
            SELECT DISTINCT ON (uri) {sel} FROM posts
            WHERE did = ANY(%s) AND seen_at >= %s AND seen_at <= %s AND deleted_at IS NULL
            ORDER BY uri, seen_at
            """,
            (dids, ws, we),
        ).fetchall()
    }
    for r in conn.execute(  # snapshots outlive raw-table retention
        f"""
        SELECT {", ".join("s." + x for x in pcols)} FROM cluster_posts cp
        JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
        WHERE cp.cluster_id = %s AND s.deleted_at IS NULL
        """,
        (cluster_id,),
    ).fetchall():
        posts.setdefault(r[0], dict(zip(pcols, r, strict=True)))
    actions = [
        {"kind": r[0], "did": r[1], "subject_uri": r[2], "subject_did": r[3], "seen_at": r[4]}
        for r in conn.execute(
            """
            SELECT 'like', did, subject_uri, subject_did, seen_at FROM likes
             WHERE did = ANY(%(d)s) AND seen_at >= %(s)s AND seen_at <= %(e)s AND deleted_at IS NULL
            UNION ALL
            SELECT 'repost', did, subject_uri, subject_did, seen_at FROM reposts
             WHERE did = ANY(%(d)s) AND seen_at >= %(s)s AND seen_at <= %(e)s AND deleted_at IS NULL
            UNION ALL
            SELECT 'follow', did, NULL, subject_did, seen_at FROM follows
             WHERE did = ANY(%(d)s) AND seen_at >= %(s)s AND seen_at <= %(e)s AND deleted_at IS NULL
            """,
            {"d": dids, "s": ws, "e": we},
        ).fetchall()
    ]
    subj = Counter(a["subject_uri"] for a in actions if a["subject_uri"])
    subj.update(p["reply_to"] for p in posts.values() if p["reply_to"])
    subj.update(p["quote_uri"] for p in posts.values() if p["quote_uri"])
    top = [u for u, _ in subj.most_common(40)]
    subject_texts = {}
    if top:
        for r in conn.execute(
            """
            SELECT DISTINCT ON (uri) uri, did, text FROM posts WHERE uri = ANY(%s)
            UNION ALL
            SELECT uri, did, text FROM cluster_posts_snapshot WHERE uri = ANY(%s)
            """,
            (top, top),
        ).fetchall():
            subject_texts.setdefault(r[0], {"did": r[1], "text": r[2]})
    ev = build_evidence(
        ws, we, det["window_kind"], members, list(posts.values()), actions, subject_texts
    )
    det = {
        **det,
        "score": round(float(det["score"]), 4),
        "confidence": None if det["confidence"] is None else round(float(det["confidence"]), 4),
        "p_value": None if det["p_value"] is None else float(det["p_value"]),
        "window_start": _iso(ws),
        "window_end": _iso(we),
        "updated_at": _iso(det["updated_at"]),
    }
    return det, ev


def build_sample(
    conn, name: str, n: int, seed: int, settled_min: float, max_age_h: float, source: str
) -> dict:
    cands = fetch_candidates(conn, settled_min, max_age_h)
    reps = one_per_campaign(cands, random.Random(seed))
    picked, strata = stratified_sample(reps, n, seed)
    items = []
    for k, p in enumerate(picked, 1):
        det, ev = fetch_cluster(conn, p["candidate"].cluster_id)
        items.append(
            {
                "item": k,
                "stratum": p["stratum"],
                "weight": round(p["weight"], 6),
                "split": split_of(p["candidate"].window_start),
                "detector": det,
                "evidence": ev,
            }
        )
        if k % 10 == 0:
            log.info("%d/%d clusters snapshotted", k, len(picked))
    return {
        "name": name,
        "rule_version": SAMPLE_RULE_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "seed": seed,
        "eval_commit": _git_sha(),
        "rule": (
            f"clusters not updated for >= {settled_min:g} min and starting within the last "
            f"{max_age_h:g} h (raw actions still retained), synthetic excluded; one random "
            "episode per campaign; stratified by detector status (target shares "
            f"{STATUS_SHARE}) x 4 equal-count score bands; weight = stratum population / sampled"
        ),
        "population": {
            "clusters": len(cands),
            "campaigns": len(reps),
            "by_status": dict(Counter(c.status for c in reps)),
        },
        "strata": strata,
        "detector_versions": dict(Counter(i["detector"]["detector_version"] for i in items)),
        "items": items,
    }


# ---------------------------------------------------------------- label store


def append_record(path: Path, rec: dict) -> dict:
    """Append one label/adjudication record (JSONL, flock'd, fsync'd)."""
    if rec.get("label") not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **rec}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    return rec


def load_records(path: Path) -> tuple[dict[str, dict[int, dict]], dict[int, dict]]:
    """-> (labels[labeller][item] = latest record, adjudications[item] = latest record)."""
    labels: dict[str, dict[int, dict]] = defaultdict(dict)
    adj: dict[int, dict] = {}
    if not path.exists():
        return {}, {}
    with open(path, encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)  # writers hold LOCK_EX: never read half a record
        text = f.read()
    for line in text.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") == "adjudication":
            adj[int(r["item"])] = r
        else:
            prev = labels[r["labeller"]].get(int(r["item"]))
            r["first_label"] = prev["first_label"] if prev else r["label"]
            labels[r["labeller"]][int(r["item"])] = r
    return dict(labels), adj


def clean_name(name: str) -> str:
    s = "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch in "-_.")
    if not s or len(s) > 32:
        raise ValueError("labeller name: 1-32 letters, digits, - _ .")
    return s


# ---------------------------------------------------------------- scoring


def weighted_rate(hits: list[tuple[bool, float]]) -> dict:
    """Rate of True among (hit, weight) pairs, weighted, with a Wilson interval on the Kish
    effective sample size n_eff = (sum w)^2 / sum w^2. Equal weights give the plain k/n."""
    n = len(hits)
    k = sum(h for h, _ in hits)
    if n == 0:
        return {"k": 0, "n": 0, "rate": None, "ci95": None, "n_eff": 0}
    sw = sum(w for _, w in hits)
    rate = sum(w for h, w in hits if h) / sw
    n_eff = sw * sw / sum(w * w for _, w in hits)
    return {
        "k": k,
        "n": n,
        "rate": round(rate, 4),
        "ci95": wilson(rate * n_eff, n_eff),
        "n_eff": round(n_eff, 1),
    }


def cohen_kappa(pairs: list[tuple[str, str]], categories: tuple[str, ...] = LABELS) -> dict:
    """Cohen's kappa for two raters, with the large-sample approximate SE
    sqrt(po(1-po) / (n (1-pe)^2)) and a 95% interval clipped to [-1, 1]."""
    n = len(pairs)
    if n == 0:
        return {"kappa": None, "n": 0, "observed": None, "expected": None, "ci95": None}
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in categories) / (n * n)
    if pe >= 1.0:  # both raters used one category for everything: kappa undefined
        return {"kappa": None, "n": n, "observed": round(po, 4), "expected": 1.0, "ci95": None}
    kappa = (po - pe) / (1 - pe)
    se = math.sqrt(po * (1 - po) / (n * (1 - pe) ** 2))
    return {
        "kappa": round(kappa, 4),
        "n": n,
        "observed": round(po, 4),
        "expected": round(pe, 4),
        "ci95": (round(max(-1.0, kappa - 1.96 * se), 4), round(min(1.0, kappa + 1.96 * se), 4)),
    }


def pick_pair(labels: dict[str, dict[int, dict]]) -> tuple[str, str] | None:
    """The two labellers who labelled the most items in common (ties: by name)."""
    names = sorted(labels)
    best = None
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            common = len(labels[a].keys() & labels[b].keys())
            if best is None or common > best[0]:
                best = (common, a, b)
    return (best[1], best[2]) if best else None


def final_labels(
    items: list[int], labels: dict[str, dict[int, dict]], adj: dict[int, dict]
) -> dict[int, dict]:
    """item -> {label|None, how}. An adjudication wins; else all labellers agree (>= 2 of them);
    else unresolved ('single' = one labeller so far, 'disagree' = needs adjudication)."""
    out = {}
    for it in items:
        if it in adj:
            out[it] = {"label": adj[it]["label"], "how": "adjudicated"}
            continue
        given = [labels[name][it]["label"] for name in labels if it in labels[name]]
        if len(given) >= 2 and len(set(given)) == 1:
            out[it] = {"label": given[0], "how": "agreed"}
        elif len(given) >= 2:
            out[it] = {"label": None, "how": "disagree"}
        elif len(given) == 1:
            out[it] = {"label": None, "how": "single"}
        else:
            out[it] = {"label": None, "how": "unlabelled"}
    return out


def _metrics(items: list[dict], final: dict[int, dict]) -> dict:
    """Precision of `coordinated`, FPR, and what humans say about the other verdicts, over
    items whose final human label is coordinated or organic. Weights undo the oversampling."""
    use = [(i, final[i["item"]]["label"]) for i in items if final[i["item"]]["label"] in DEFINITE]
    flagged = [
        (lab == "coordinated", i["weight"])
        for i, lab in use
        if i["detector"]["status"] == "coordinated"
    ]
    organic = [
        (i["detector"]["status"] == "coordinated", i["weight"])
        for i, lab in use
        if lab == "organic"
    ]
    chill = [
        (lab == "coordinated", i["weight"])
        for i, lab in use
        if i["detector"]["status"] == "likely_organic"
    ]
    abstain = [
        (lab == "coordinated", i["weight"])
        for i, lab in use
        if i["detector"]["status"] == "insufficient"
    ]
    cant = [i for i in items if final[i["item"]]["label"] == "cant_tell"]
    return {
        "n_definite": len(use),
        "n_cant_tell": len(cant),
        "precision": weighted_rate(flagged),
        "fpr": weighted_rate(organic),
        "coordinated_among_chill": weighted_rate(chill),
        "coordinated_among_cant_tell_verdicts": weighted_rate(abstain),
        "confusion": dict(Counter(f"{i['detector']['status']}->{lab}" for i, lab in use)),
    }


def score(sample: dict, labels: dict[str, dict[int, dict]], adj: dict[int, dict]) -> dict:
    """Final labels and kappa use the labelling pair only (the server's disagreement view uses
    the same pair), so a stray name can't leave items unresolvable. Kappa uses each person's
    FIRST label per item: a change made after seeing the other's label must not raise it."""
    items = sample["items"]
    ids = [i["item"] for i in items]
    pair = pick_pair(labels)
    final = final_labels(ids, {n: labels[n] for n in pair} if pair else {}, adj)
    agreement = None
    if pair:
        a, b = pair
        common = sorted(labels[a].keys() & labels[b].keys() & set(ids))

        def first(r: dict) -> str:
            return r.get("first_label", r["label"])

        three = [(first(labels[a][i]), first(labels[b][i])) for i in common]
        two = [(x, y) for x, y in three if x in DEFINITE and y in DEFINITE]
        agreement = {
            "labellers": [a, b],
            "kappa_3way": cohen_kappa(three, LABELS),
            "kappa_definite": cohen_kappa(two, DEFINITE),
            "raw_agreement": weighted_rate([(x == y, 1.0) for x, y in three]),
        }
    by_split = {
        s: _metrics([i for i in items if i["split"] == s], final) for s in ("dev", "heldout")
    }
    return {
        "sample": sample["name"],
        "sample_rule": sample.get("rule_version"),
        "detector_versions": sample.get("detector_versions"),
        "n_items": len(items),
        "labellers": {name: len(v.keys() & set(ids)) for name, v in sorted(labels.items())},
        "resolution": dict(Counter(f["how"] for f in final.values())),
        "agreement": agreement,
        "all": _metrics(items, final),
        "by_split": by_split,
        "final": {str(k): v for k, v in final.items()},
    }


def format_report(r: dict) -> str:
    def rate(m: dict) -> str:
        if m["rate"] is None:
            return "n/a (n=0)"
        lo, hi = m["ci95"]
        return f"{m['rate']:.3f}  [{lo:.3f}, {hi:.3f}]  k={m['k']} n={m['n']} n_eff={m['n_eff']}"

    lines = [
        f"sample {r['sample']} ({r['n_items']} items, detector {r['detector_versions']})",
        f"labels per labeller: {r['labellers']}",
        f"resolution: {r['resolution']}",
    ]
    ag = r["agreement"]
    if ag:
        for key in ("kappa_3way", "kappa_definite"):
            k = ag[key]
            ci = f" [{k['ci95'][0]:.3f}, {k['ci95'][1]:.3f}]" if k["ci95"] else ""
            kv = "n/a" if k["kappa"] is None else f"{k['kappa']:.3f}"
            who = " vs ".join(ag["labellers"])
            lines.append(f"{key} ({who}): {kv}{ci} n={k['n']}")
    for name, m in [("all", r["all"]), *r["by_split"].items()]:
        lines += [
            f"-- {name}: {m['n_definite']} definite, {m['n_cant_tell']} can't tell",
            f"   precision(coordinated)       {rate(m['precision'])}",
            f"   false-positive rate          {rate(m['fpr'])}",
            f"   coordinated among Chill      {rate(m['coordinated_among_chill'])}",
            f"   coordinated among Can't tell {rate(m['coordinated_among_cant_tell_verdicts'])}",
        ]
    return "\n".join(lines)


def eval_rows(r: dict, sample: dict) -> list[dict]:
    """One eval_runs row per split, so the heldout number never mixes with dev."""
    rows = []
    for split, m in r["by_split"].items():
        if m["n_definite"] == 0:
            continue
        rows.append(
            {
                "dataset": DATASET,
                "split": split,
                "precision": m["precision"]["rate"],
                "fpr": m["fpr"]["rate"],
                "n": m["n_definite"],
                "details": {
                    "synthetic": False,
                    "what": "two humans label a frozen stratified sample of live clusters "
                    "(blind to detector status and score); precision of 'coordinated' and FPR "
                    "against the agreed/adjudicated label",
                    "sample": r["sample"],
                    "sample_rule": sample.get("rule"),
                    "sample_source": sample.get("source"),
                    "sample_created_at": sample.get("created_at"),
                    "detector_versions": r["detector_versions"],
                    "eval_commit": _git_sha(),
                    "split_rule": "window_start UTC hour: odd = heldout, even = dev",
                    "metrics": m,
                    "agreement": r["agreement"],
                    "resolution": r["resolution"],
                    "labellers": r["labellers"],
                    "population": sample.get("population"),
                    "strata": sample.get("strata"),
                    "items": [
                        {
                            "item": i["item"],
                            "cluster_id": i["detector"]["cluster_id"],
                            "status": i["detector"]["status"],
                            "score": i["detector"]["score"],
                            "weight": i["weight"],
                            "final": r["final"][str(i["item"])],
                        }
                        for i in sample["items"]
                        if i["split"] == split
                    ],
                    "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            }
        )
    return rows


# ---------------------------------------------------------------- CLI


def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("sample", help="freeze a stratified sample of live clusters")
    s.add_argument("--name", required=True, help="sample name, e.g. live-v1 (never reused)")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=20261113)
    s.add_argument(
        "--settled-min",
        type=float,
        default=15.0,
        help="only clusters not updated for this many minutes",
    )
    s.add_argument(
        "--max-age-h",
        type=float,
        default=10.0,
        help="only windows that started this recently (likes are kept 12 h)",
    )
    s.add_argument("--source", default="live", help="free text: which deployment was read")
    s.add_argument("--database-url", help="DB to read (default: DATABASE_URL / .env)")
    v = sub.add_parser("serve", help="labelling web page")
    v.add_argument("--name", help="sample to label (default: newest)")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    c = sub.add_parser("score", help="kappa, precision, FPR")
    c.add_argument("--name", required=True)
    c.add_argument("--json-out", help="also write the report as JSON")
    c.add_argument("--database-url", help="write eval_runs rows here (nothing written if unset)")


def main_args(a: argparse.Namespace) -> int:
    if a.action == "sample":
        out = sample_path(a.name)
        if out.exists():
            print(f"{out} exists; samples are frozen, pick a new --name", file=sys.stderr)
            return 2
        import psycopg

        url = a.database_url
        if url is None:
            from common.config import get_settings

            url = get_settings().database_url
        with psycopg.connect(url, connect_timeout=10) as conn:
            conn.read_only = True
            sample = build_sample(conn, a.name, a.n, a.seed, a.settled_min, a.max_age_h, a.source)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(sample, ensure_ascii=False, indent=1, default=str))
        tmp.rename(out)
        print(f"wrote {out}: {len(sample['items'])} items; population {sample['population']}")
        for key, st in sample["strata"].items():
            print(
                f"  {key:22s} score {st['score_min']:.2f}-{st['score_max']:.2f}  "
                f"{st['sampled']:3d} of {st['population']}"
            )
        return 0
    if a.action == "serve":
        import uvicorn

        from eval.label_server import create_app

        uvicorn.run(create_app(a.name), host=a.host, port=a.port, log_level="info")
        return 0
    if a.action == "score":
        sample = json.loads(sample_path(a.name).read_text())
        labels, adj = load_records(labels_path(a.name))
        r = score(sample, labels, adj)
        print(format_report(r))
        if a.json_out:
            Path(a.json_out).write_text(json.dumps(r, indent=1))
        if a.database_url:
            from eval.store import write_eval_run

            rows = eval_rows(r, sample)
            if not rows:
                print("no adjudicated definite labels yet; nothing written", file=sys.stderr)
                return 2
            for row in rows:
                rid = write_eval_run(database_url=a.database_url, **row)
                print(f"wrote eval_runs id={rid} (dataset={DATASET}, split={row['split']})")
        return 0
    return 2
