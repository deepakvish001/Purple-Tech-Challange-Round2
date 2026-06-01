"""Smoke test for the API stub — no Redis required."""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.api.main import app


def test_healthz() -> None:
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
