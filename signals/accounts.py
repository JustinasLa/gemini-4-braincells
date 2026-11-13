"""Load AccountInfo for cluster members from the accounts table (filled by signals.enrich)."""

from datetime import UTC, datetime

import psycopg

from signals.score import AccountInfo


def load_account_info(
    conn: psycopg.Connection, dids: list[str], now: datetime | None = None
) -> dict[str, AccountInfo]:
    if not dids:
        return {}
    now = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT did, handle, display_name, least(did_created_at, created_at) AS born, followers,
               follows, posts_count, description_len, has_avatar, pds_host, handle_changes,
               profile_fetched_at IS NOT NULL OR plc_fetched_at IS NOT NULL AS enriched
        FROM accounts WHERE did = ANY(%s)
        """,
        (dids,),
    ).fetchall()
    out = {}
    for r in rows:
        if not r[11]:
            continue
        out[r[0]] = AccountInfo(
            did=r[0],
            handle=r[1],
            display_name=r[2],
            age_days=(now - r[3]).total_seconds() / 86400 if r[3] else None,
            followers=r[4],
            follows=r[5],
            posts_count=r[6],
            description_len=r[7],
            has_avatar=r[8],
            pds_host=r[9],
            handle_changes=r[10],
        )
    return out
