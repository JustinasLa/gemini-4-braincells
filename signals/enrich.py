"""Account enrichment for cluster candidates only (never the whole stream).

- AppView app.bsky.actor.getProfiles (public.api.bsky.app, <= 25 actors per call): handle, display
  name, profile createdAt, follower/follow/post counts, avatar, bio length.
- PLC audit log (plc.directory/<did>/log/audit): DID creation time (first operation), number of
  handle changes, PDS host. did:web accounts have no PLC log.
Results are cached in `accounts` (profile_fetched_at / plc_fetched_at) and refreshed after a TTL.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import psycopg

log = logging.getLogger(__name__)

APPVIEW = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
PLC = "https://plc.directory"
PROFILE_TTL_H = 6
UA = {"User-Agent": "noise-radar-research/0.1 (read-only)"}


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _get(client: httpx.Client, url: str, params=None, tries: int = 3):
    for i in range(tries):
        try:
            r = client.get(url, params=params, timeout=10)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            return r
        except (httpx.HTTPError, httpx.TimeoutException):
            if i == tries - 1:
                return None
            time.sleep(0.5 * 2**i)
    return None


def fetch_profiles(client: httpx.Client, dids: list[str], workers: int = 4) -> dict[str, dict]:
    """getProfiles in chunks of 25 (the AppView limit), a few chunks in parallel."""
    chunks = [dids[i : i + 25] for i in range(0, len(dids), 25)]

    def one(chunk: list[str]) -> list[dict]:
        r = _get(client, APPVIEW, params=[("actors", d) for d in chunk])
        if r is None or r.status_code != 200:
            return []
        return r.json().get("profiles", [])

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for profiles in pool.map(one, chunks):
            for p in profiles:
                out[p["did"]] = p
    return out


def fetch_plc(client: httpx.Client, did: str) -> dict | None:
    if not did.startswith("did:plc:"):
        return None
    r = _get(client, f"{PLC}/{did}/log/audit")
    if r is None or r.status_code != 200:
        return None
    ops = [op for op in r.json() if not op.get("nullified")]
    if not ops:
        return None
    handles, pds = [], None
    for op in ops:
        o = op.get("operation", {})
        aka = o.get("alsoKnownAs") or ([f"at://{o['handle']}"] if o.get("handle") else [])
        if aka and (not handles or handles[-1] != aka[0]):
            handles.append(aka[0])
        ep = (o.get("services", {}).get("atproto_pds", {}) or {}).get("endpoint") or o.get(
            "service"
        )
        if ep:
            pds = urlsplit(ep).hostname
    return {
        "created": _ts(ops[0].get("createdAt")),
        "handle_changes": max(0, len(handles) - 1),
        "pds_host": pds,
    }


def enrich(conn: psycopg.Connection, dids: list[str], budget: int = 400) -> int:
    """Fetch missing/stale profile + PLC data for up to `budget` of these DIDs. Returns count."""
    if not dids:
        return 0
    rows = conn.execute(
        """
        SELECT did FROM accounts WHERE did = ANY(%s) AND (
            profile_fetched_at IS NULL OR profile_fetched_at < now() - make_interval(hours => %s)
            OR (plc_fetched_at IS NULL AND did LIKE 'did:plc:%%')
            OR (description IS NULL AND coalesce(description_len, 0) > 0))
        """,
        (dids, PROFILE_TTL_H),
    ).fetchall()
    stale = {r[0] for r in rows}
    todo = [d for d in dids if d in stale][:budget]  # caller's priority order
    if not todo:
        return 0
    with httpx.Client(headers=UA, follow_redirects=True) as client:
        profiles = fetch_profiles(client, todo)
        with ThreadPoolExecutor(max_workers=8) as pool:
            plc = dict(zip(todo, pool.map(lambda d: fetch_plc(client, d), todo), strict=True))
    now = datetime.now(UTC)
    with conn.transaction():
        for d in todo:
            p = profiles.get(d)
            q = plc.get(d)
            conn.execute(
                """
                UPDATE accounts SET
                    handle = coalesce(%(handle)s, handle),
                    display_name = coalesce(%(dn)s, display_name),
                    created_at = coalesce(%(created)s, created_at),
                    followers = coalesce(%(followers)s, followers),
                    follows = coalesce(%(follows)s, follows),
                    posts_count = coalesce(%(posts)s, posts_count),
                    description_len = coalesce(%(desc)s, description_len),
                    description = coalesce(%(descr)s, description),
                    has_avatar = coalesce(%(avatar)s, has_avatar),
                    profile_fetched_at = %(now)s,
                    did_created_at = coalesce(%(plc_created)s, did_created_at),
                    handle_changes = coalesce(%(hc)s, handle_changes),
                    pds_host = coalesce(%(pds)s, pds_host),
                    plc_fetched_at = CASE WHEN %(plc_ok)s THEN %(now)s ELSE plc_fetched_at END
                WHERE did = %(did)s
                """,
                {
                    "did": d,
                    "now": now,
                    "plc_ok": q is not None,
                    "handle": p.get("handle") if p else None,
                    "dn": (p.get("displayName") or "")[:200] if p else None,
                    "created": _ts(p.get("createdAt")) if p else None,
                    "followers": p.get("followersCount") if p else None,
                    "follows": p.get("followsCount") if p else None,
                    "posts": p.get("postsCount") if p else None,
                    "desc": len(p.get("description") or "") if p else None,
                    "descr": (p.get("description") or "")[:1000] if p else None,
                    "avatar": bool(p.get("avatar")) if p else None,
                    "plc_created": q["created"] if q else None,
                    "hc": q["handle_changes"] if q else None,
                    "pds": q["pds_host"] if q else None,
                },
            )
    log.info(
        "enriched %d accounts (%d profiles, %d plc)",
        len(todo),
        len(profiles),
        sum(1 for v in plc.values() if v),
    )
    return len(todo)
