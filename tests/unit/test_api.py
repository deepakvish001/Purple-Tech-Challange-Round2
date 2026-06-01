"""API health smoke + DB-degraded behaviour.

The full DB-backed paths are covered by tests/integration/ once a real
Postgres is available; unit tests just verify graceful degradation.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.api.main import app


def test_healthz() -> None:
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_metrics_returns_503_when_db_unavailable() -> None:
    with TestClient(app) as c:
        app.state.pool = None  # force the degraded path
        r = c.get("/metrics")
        assert r.status_code == 503
