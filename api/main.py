"""FastAPI backend. Routes and their JSON shapes are listed in docs/ARCHITECTURE.md (Shared interfaces)."""

import logging
from datetime import UTC, datetime

from fastapi import FastAPI

from api.routes import analyze, clusters, feed, lookup, metrics, swarms
from common.db import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Shill or Chill API", version="0.2.0")
app.include_router(clusters.router)
app.include_router(metrics.router)
app.include_router(analyze.router)
app.include_router(lookup.router)
app.include_router(feed.router)
app.include_router(swarms.router)


@app.get("/api/health")
def health() -> dict:
    """{ok, ingest_lag_s, last_event_at}. ok=false (not an error) when the DB is unreachable."""
    try:
        with connect() as conn:
            row = conn.execute("SELECT max(ingested_at) AS last FROM posts").fetchone()
    except Exception as exc:  # DB down or schema missing: report it, don't crash the probe
        return {
            "ok": False,
            "ingest_lag_s": None,
            "last_event_at": None,
            "error": type(exc).__name__,
        }

    last = row["last"] if row else None
    lag = (datetime.now(UTC) - last).total_seconds() if last else None
    return {
        "ok": True,
        "ingest_lag_s": round(lag, 1) if lag is not None else None,
        "last_event_at": last.isoformat() if last else None,
    }
