"""FastAPI dependencies. Tests override these with fakes (app.dependency_overrides)."""

from collections.abc import Iterator

import psycopg
from fastapi import HTTPException

from api.store import Store
from common.config import get_settings
from common.db import connect
from sources.checker import SourceChecker
from sources.oxylabs import OxylabsClient
from swarm.store import SwarmStore


def get_store() -> Iterator[Store]:
    try:
        with connect() as conn:
            yield Store(conn)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "database unavailable") from exc


def get_swarm_store() -> Iterator[SwarmStore]:
    """meme_swarms reads (api/routes/swarms.py); a missing table reads as empty."""
    try:
        with connect() as conn:
            yield SwarmStore(conn)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "database unavailable") from exc


_checker: SourceChecker | None = None


def get_checker() -> SourceChecker | None:
    """Live Oxylabs checks for /api/analyze. None when no key is configured."""
    global _checker
    if _checker is None and get_settings().oxylabs_web_api_key:
        _checker = SourceChecker(OxylabsClient.from_settings())
    return _checker
