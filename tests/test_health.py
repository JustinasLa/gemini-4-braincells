from fastapi.testclient import TestClient

from api.main import app


def test_health_reports_db_down_without_crashing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none")
    from common.config import get_settings

    get_settings.cache_clear()
    res = TestClient(app).get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["ingest_lag_s"] is None
    get_settings.cache_clear()
