"""FastAPI dependencies. Tests override these with fakes (app.dependency_overrides)."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

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


@contextmanager
def swarm_store_session() -> Iterator[SwarmStore]:
    """A meme_swarms reader on its own short connection; a missing table reads as empty."""
    try:
        with connect() as conn:
            yield SwarmStore(conn)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "database unavailable") from exc


def get_swarm_store() -> Iterator[SwarmStore]:
    """meme_swarms reads (api/routes/swarms.py), connection held for the request."""
    with swarm_store_session() as store:
        yield store


def get_swarm_sessions() -> Callable[[], AbstractContextManager[SwarmStore]]:
    """Opens a reader only for as long as a `with` needs it: the picture route lets the
    connection go before it waits on the CDN."""
    return swarm_store_session


_checker: SourceChecker | None = None


def get_checker() -> SourceChecker | None:
    """Live Oxylabs checks for /api/analyze. None when no key is configured."""
    global _checker
    if _checker is None and get_settings().oxylabs_web_api_key:
        _checker = SourceChecker(OxylabsClient.from_settings())
    return _checker
