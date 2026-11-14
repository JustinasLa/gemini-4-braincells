"""Failed profile lookups must not start the six-hour profile cache TTL."""

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import httpx
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


def test_failed_chunk_only_refreshes_successful_accounts(monkeypatch):
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


def test_successful_response_caches_absent_accounts_without_inventing_profile_fields(monkeypatch):
    did = "did:web:deleted.example"
    conn = Connection([did])
    monkeypatch.setattr(enrichment, "fetch_profiles", lambda client, dids: {did: None})
    monkeypatch.setattr(enrichment, "fetch_plc", lambda client, did: None)
    enrichment.enrich(conn, [did])
    assert isinstance(conn.updates[did]["profile_fetched"], datetime)
    assert conn.updates[did]["handle"] is None
    assert conn.updates[did]["followers"] is None


def test_profile_fetch_distinguishes_absence_from_failed_chunks(monkeypatch):
    dids = [f"did:web:actor{i}.example" for i in range(26)]

    def response(client, url, params):
        chunk = [did for _, did in params]
        if chunk == dids[25:]:
            return None
        return httpx.Response(200, json={"profiles": [{"did": dids[0]}]})

    monkeypatch.setattr(enrichment, "_get", response)
    result = enrichment.fetch_profiles(None, dids)
    assert result[dids[0]] == {"did": dids[0]}
    assert result[dids[1]] is None
    assert len(result) == 25
    assert dids[25] not in result


@pytest.mark.parametrize("body", [b"not JSON", b"{}", b"[]"])
def test_malformed_profile_response_remains_retryable(monkeypatch, body):
    monkeypatch.setattr(
        enrichment, "_get", lambda *args, **kwargs: httpx.Response(200, content=body)
    )
    assert enrichment.fetch_profiles(None, ["did:web:actor.example"]) == {}
