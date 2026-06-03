"""DB-backed FastAPI surface.

`/metrics`, `/funnel`, `/anomalies`, `/zones`, `/sessions/{id}`, `/cameras`
all read from the materialised aggregator tables. `/events/recent` keeps
its stream-tail behaviour as a debug window.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from services.aggregator import db
from services.events import EventBus

REDIS_URL    = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://store:store@postgres:5432/store")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bus = EventBus(REDIS_URL)
    try:
        app.state.pool = await db.open_pool(DATABASE_URL)
    except Exception:  # noqa: BLE001
        app.state.pool = None  # API stays up, /readyz reports the failure
    try:
        yield
    finally:
        if app.state.pool is not None:
            await app.state.pool.close()
        await app.state.bus.close()


app = FastAPI(title="Store Intelligence API", version="0.3.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    redis_ok = False
    db_ok = False
    try:
        await app.state.bus.ping()
        redis_ok = True
    except Exception:  # noqa: BLE001
        pass
    if app.state.pool is not None:
        try:
            async with app.state.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
            db_ok = True
        except Exception:  # noqa: BLE001
            pass
    if not (redis_ok and db_ok):
        raise HTTPException(503, detail={"redis": redis_ok, "postgres": db_ok})
    return {"status": "ready", "redis": "ok", "postgres": "ok"}


def _pool_or_503(app: FastAPI):
    if app.state.pool is None:
        raise HTTPException(503, detail="database_unavailable")
    return app.state.pool


@app.get("/metrics")
async def metrics(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    m = await db.fetch_recent_metrics(pool, hours=hours)
    footfall = int(m["footfall"])
    purchases = int(m["purchases"])
    return {
        "computed_at":            datetime.now(UTC).isoformat(),
        "window_hours":           hours,
        "footfall":               footfall,
        "unique_visitors":        int(m["unique_visitors"]),
        "purchases":              purchases,
        "checkouts_observed":     int(m["checkouts"]),
        "conversion_rate":        (purchases / footfall) if footfall else 0.0,
        "avg_session_duration_s": float(m["avg_session_duration_s"] or 0),
        "revenue_inr":            float(m.get("revenue") or 0),
        "avg_basket_inr":         float(m.get("avg_basket") or 0),
        "items_sold":             int(m.get("items_sold") or 0),
    }


@app.get("/funnel")
async def funnel(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    stages = await db.fetch_funnel(pool, hours=hours)
    return {
        "computed_at":  datetime.now(UTC).isoformat(),
        "window_hours": hours,
        "stages":       stages,
    }


@app.get("/hourly")
async def hourly(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    rows = await db.fetch_hourly_breakdown(pool, hours=hours)
    return {"hours": rows}


@app.get("/anomalies")
async def anomalies(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    rows = await db.fetch_anomalies(pool, hours=hours)
    return {"count": len(rows), "anomalies": rows}


@app.get("/zones")
async def zones(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    return {"zones": await db.fetch_zone_summary(pool, hours=hours)}


@app.get("/activity")
async def activity(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    pool = _pool_or_503(app)
    return {"sessions": await db.fetch_recent_purchases(pool, limit=limit)}


@app.get("/sessions/{session_id}")
async def session_get(session_id: str) -> dict[str, Any]:
    pool = _pool_or_503(app)
    row = await db.fetch_session(pool, session_id)
    if not row:
        raise HTTPException(404, detail="session_not_found")
    return row


@app.get("/cameras")
async def cameras() -> dict[str, Any]:
    pool = _pool_or_503(app)
    return {"cameras": await db.fetch_cameras_health(pool)}


@app.get("/events/recent")
async def events_recent(n: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    recent = await app.state.bus.recent(n=n)
    return {"count": len(recent), "events": [e.model_dump(mode="json") for e in recent]}


@app.get("/metrics-prom", response_class=PlainTextResponse)
async def metrics_prom() -> Any:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
