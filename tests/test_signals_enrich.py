"""Failed profile lookups must not start the six-hour profile cache TTL."""

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

import signals.enrich as enrichment


class Connection:
    def __init__(self, dids):
        self.dids = dids
        self.updates = {}

    def execute(self, sql, params):
        if "SELECT did" in sql:
            return SimpleNamespace(fetchall=lambda: [(did,) for did in self.dids])
        self.updates[params["did"]] = params

    def transaction(self):
        return nullcontext()


@pytest.mark.parametrize("plc_ok", [False, True])
def test_failed_profile_lookup_does_not_refresh_its_timestamp(monkeypatch, plc_ok):
    did = "did:plc:missing"
    conn = Connection([did])
    monkeypatch.setattr(enrichment, "fetch_profiles", lambda client, dids: {})
    plc = {"created": None, "handle_changes": 0, "pds_host": "pds.example"}
    monkeypatch.setattr(enrichment, "fetch_plc", lambda client, did: plc if plc_ok else None)

    enrichment.enrich(conn, [did])

    update = conn.updates[did]
    # NULL passed to coalesce preserves the old timestamp, including a never-fetched NULL.
    # A successful PLC lookup must not make a failed AppView lookup fresh.
    assert update["profile_fetched"] is None
    assert update["plc_ok"] is plc_ok
    assert update["handle"] is None and update["avatar"] is None


def test_partial_profile_response_only_refreshes_returned_profiles(monkeypatch):
    found, missing = "did:web:found.example", "did:web:missing.example"
    conn = Connection([found, missing])
    monkeypatch.setattr(
        enrichment,
        "fetch_profiles",
        lambda client, dids: {found: {"did": found, "handle": "found.example"}},
    )
    monkeypatch.setattr(enrichment, "fetch_plc", lambda client, did: None)

    enrichment.enrich(conn, [found, missing])

    assert isinstance(conn.updates[found]["profile_fetched"], datetime)
    assert conn.updates[found]["handle"] == "found.example"
    assert conn.updates[missing]["profile_fetched"] is None

    # A later successful fetch may start the TTL for the previously missing profile.
    monkeypatch.setattr(
        enrichment,
        "fetch_profiles",
        lambda client, dids: {missing: {"did": missing, "handle": "missing.example"}},
    )
    enrichment.enrich(conn, [missing])
    assert isinstance(conn.updates[missing]["profile_fetched"], datetime)
    assert conn.updates[missing]["handle"] == "missing.example"
