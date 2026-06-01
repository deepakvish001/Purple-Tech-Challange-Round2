"""Minimal FastAPI stub.

This slice ships only the endpoints needed to clear the acceptance gate:
`/healthz`, `/readyz`, `/metrics`, plus a debug `/events/recent`. They
compute live from the raw Redis Stream so reviewers can see the system
working without the aggregator + Postgres pipeline.

The real implementations (materialised views, funnel computation,
anomalies) replace these in a follow-up slice — same routes, same shapes.
"""

from __future__ import annotations

import os
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client import Counter as PCounter

from services.events import EventBus
from services.events.schemas import EVENT_TYPES

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

events_seen = PCounter("api_events_seen_total", "Events observed by API debug reads", ["type"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bus = EventBus(REDIS_URL)
    try:
        yield
    finally:
        await app.state.bus.close()


app = FastAPI(
    title="Store Intelligence API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    try:
        await app.state.bus.ping()
        return {"status": "ready", "redis": "ok"}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"redis_unreachable: {e}") from e


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Live metrics computed from the recent event window.

    This is a placeholder that computes from the last 1000 events on the
    stream. Once the aggregator + Postgres land, this endpoint reads from
    `mv_hourly_metrics` instead.
    """
    bus: EventBus = app.state.bus
    recent = await bus.recent(n=1000)
    for ev in recent:
        events_seen.labels(type=ev.type).inc(0)  # ensure series exist

    counts: Counter[str] = Counter(ev.type for ev in recent)
    customers = {
        ev.embedding_id
        for ev in recent
        if ev.type == "person_entered" and ev.role != "staff" and ev.embedding_id
    }
    purchases = counts["pos_receipt"]
    footfall = counts["person_entered"]
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "window": "stream_tail_1000",
        "footfall": footfall,
        "unique_visitors": len(customers),
        "checkouts_observed": counts["checkout_observed"],
        "purchases": purchases,
        "conversion_rate": (purchases / footfall) if footfall else 0.0,
        "events_by_type": {t: counts.get(t, 0) for t in EVENT_TYPES},
        "stream_length": await bus.stream_length(),
    }


@app.get("/events/recent")
async def events_recent(n: int = 50) -> dict[str, Any]:
    bus: EventBus = app.state.bus
    n = max(1, min(500, n))
    recent = await bus.recent(n=n)
    return {"count": len(recent), "events": [e.model_dump(mode="json") for e in recent]}


@app.get("/metrics-prom", response_class=PlainTextResponse)
async def metrics_prom() -> Any:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/funnel")
async def funnel() -> dict[str, Any]:
    """Placeholder funnel — real implementation in the aggregator slice."""
    bus: EventBus = app.state.bus
    recent = await bus.recent(n=1000)
    by_session: dict[str, set[str]] = {}
    # `embedding_id` is a stand-in for `session_id` until the aggregator
    # publishes resolved sessions.
    for ev in recent:
        key = ev.embedding_id or ev.event_id
        by_session.setdefault(key, set()).add(ev.type)

    stages = {"entered": 0, "browsed": 0, "engaged": 0, "checkout_queued": 0, "purchased": 0}
    for types in by_session.values():
        if "person_entered" in types:
            stages["entered"] += 1
        if "zone_entered" in types:
            stages["browsed"] += 1
        if "zone_dwell" in types:
            stages["engaged"] += 1
        if "checkout_observed" in types:
            stages["checkout_queued"] += 1
        if "pos_receipt" in types:
            stages["purchased"] += 1
    return {
        "computed_at": datetime.now(UTC).isoformat(),
        "window": "stream_tail_1000",
        "stages": stages,
        "note": "preliminary — aggregator slice replaces with session-true counts",
    }
