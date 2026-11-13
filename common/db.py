"""Thin psycopg helpers. Components own their queries; this only hands out connections."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from common.config import get_settings


@contextmanager
def connect(timeout_s: int = 3) -> Iterator[psycopg.Connection]:
    with psycopg.connect(
        get_settings().database_url, connect_timeout=timeout_s, row_factory=dict_row
    ) as conn:
        yield conn
