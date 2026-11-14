"""The signals worker reopens its database connection after Postgres restarts."""

from signals.__main__ import live_connection


class FakeConn:
    def __init__(self, closed: bool) -> None:
        self.closed = closed


def test_open_connection_is_reused():
    conn = FakeConn(closed=False)
    calls = []
    assert live_connection(conn, lambda: calls.append(1)) is conn
    assert calls == []


def test_closed_connection_is_replaced():
    fresh = FakeConn(closed=False)
    assert live_connection(FakeConn(closed=True), lambda: fresh) is fresh


def test_failed_reconnect_raises_so_the_loop_retries_next_pass():
    def refuse():
        raise OSError("db is still starting")

    try:
        live_connection(FakeConn(closed=True), refuse)
    except OSError:
        return
    raise AssertionError("a failed reconnect must not be swallowed")
