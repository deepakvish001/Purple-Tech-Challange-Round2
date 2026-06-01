"""Async psycopg helpers for the aggregator and the API.

Schema is created by `infra/postgres/init.sql` (run by the Postgres image
on first boot). This module assumes those tables exist; we add idempotent
helpers for upserts and aggregations.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from services.events.schemas import Envelope

log = logging.getLogger(__name__)


async def open_pool(url: str) -> AsyncConnectionPool:
    # psycopg URLs use `postgresql://`, SQLAlchemy uses `postgresql+psycopg://`.
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False)
    await pool.open()
    return pool


@asynccontextmanager
async def acquire(pool: AsyncConnectionPool):
    async with pool.connection() as conn:
        yield conn


# --------------------------------------------------------------------------
# Writes (aggregator side)
# --------------------------------------------------------------------------


async def insert_raw_event(pool: AsyncConnectionPool, env: Envelope, session_id: str | None) -> None:
    """Append the event to raw_events. Idempotent on event_id."""
    import json
    sql = """
        INSERT INTO raw_events (event_id, type, store_id, camera_id, ts, session_id, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (event_id) DO NOTHING
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (
                env.event_id,
                env.type,
                env.store_id,
                env.camera_id,
                env.ts,
                session_id,
                json.dumps(env.payload),
            ),
        )


async def upsert_session(
    pool: AsyncConnectionPool,
    *,
    session_id: str,
    store_id: str,
    embedding_id: str | None,
    role: str,
    entered_at: datetime,
    exited_at: datetime | None,
    funnel_stage: str,
    checkout_at: datetime | None,
    receipt: dict | None,
) -> None:
    sql = """
        INSERT INTO sessions (
            session_id, store_id, embedding_id, role, entered_at, exited_at,
            funnel_stage, checkout_at,
            receipt_invoice, receipt_total, receipt_items, receipt_mode, receipt_salesperson
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (session_id) DO UPDATE SET
            role                = EXCLUDED.role,
            exited_at           = EXCLUDED.exited_at,
            funnel_stage        = EXCLUDED.funnel_stage,
            checkout_at         = EXCLUDED.checkout_at,
            receipt_invoice     = EXCLUDED.receipt_invoice,
            receipt_total       = EXCLUDED.receipt_total,
            receipt_items       = EXCLUDED.receipt_items,
            receipt_mode        = EXCLUDED.receipt_mode,
            receipt_salesperson = EXCLUDED.receipt_salesperson
    """
    r = receipt or {}
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            (
                session_id,
                store_id,
                embedding_id,
                role,
                entered_at,
                exited_at,
                funnel_stage,
                checkout_at,
                r.get("invoice_number"),
                r.get("total_amount"),
                r.get("item_count"),
                r.get("payment_mode"),
                r.get("salesperson_id"),
            ),
        )


async def upsert_zone_visit(
    pool: AsyncConnectionPool,
    *,
    session_id: str,
    zone_id: str,
    first_seen: datetime,
    last_seen: datetime,
    total_dwell_s: float,
) -> None:
    sql = """
        INSERT INTO session_zone_visits (session_id, zone_id, first_seen, last_seen, total_dwell_s)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (session_id, zone_id) DO UPDATE SET
            last_seen     = EXCLUDED.last_seen,
            total_dwell_s = EXCLUDED.total_dwell_s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, (session_id, zone_id, first_seen, last_seen, total_dwell_s))


async def insert_anomaly(
    pool: AsyncConnectionPool, *, kind: str, severity: str, details: dict, bucket_ts: datetime | None = None
) -> None:
    sql = """
        INSERT INTO anomalies (kind, severity, details, bucket_ts)
        VALUES (%s, %s, %s::jsonb, %s)
    """
    import json
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, (kind, severity, json.dumps(details), bucket_ts))


# --------------------------------------------------------------------------
# Reads (API side)
# --------------------------------------------------------------------------


async def fetch_recent_metrics(pool: AsyncConnectionPool, hours: int = 24) -> dict[str, Any]:
    sql = """
        SELECT
            COALESCE(SUM(footfall), 0)        AS footfall,
            COALESCE(SUM(unique_visitors), 0) AS unique_visitors,
            COALESCE(SUM(purchases), 0)       AS purchases,
            COALESCE(SUM(checkouts), 0)       AS checkouts,
            COALESCE(AVG(NULLIF(avg_session_duration_s, 0)), 0) AS avg_session_duration_s
        FROM hourly_metrics
        WHERE hour_bucket >= now() - (%s * interval '1 hour')
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (hours,))
        row = await cur.fetchone()
    if row is None:
        return {"footfall": 0, "unique_visitors": 0, "purchases": 0, "checkouts": 0, "avg_session_duration_s": 0.0}
    return dict(row)


async def fetch_funnel(pool: AsyncConnectionPool, hours: int = 24) -> dict[str, int]:
    sql = """
        SELECT funnel_stage, COUNT(*) AS n
        FROM sessions
        WHERE entered_at >= now() - (%s * interval '1 hour')
          AND role != 'staff'
        GROUP BY funnel_stage
    """
    stages = {"entered": 0, "browsed": 0, "engaged": 0, "checkout_queued": 0, "purchased": 0}
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (hours,))
        rows = await cur.fetchall()
    # `funnel_stage` is the *highest* stage reached; collapse to cumulative.
    raw = {r["funnel_stage"]: int(r["n"]) for r in rows}
    cumulative = {}
    order = ["entered", "browsed", "engaged", "checkout_queued", "purchased"]
    running = 0
    for s in reversed(order):
        running += raw.get(s, 0)
        cumulative[s] = running
    return {**stages, **cumulative}


async def fetch_anomalies(pool: AsyncConnectionPool, hours: int = 24) -> list[dict[str, Any]]:
    sql = """
        SELECT id, detected_at, bucket_ts, kind, severity, details
        FROM anomalies
        WHERE detected_at >= now() - (%s * interval '1 hour')
        ORDER BY detected_at DESC
        LIMIT 200
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (hours,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_zone_summary(pool: AsyncConnectionPool, hours: int = 24) -> list[dict[str, Any]]:
    sql = """
        SELECT v.zone_id,
               COUNT(DISTINCT v.session_id)              AS unique_visitors,
               COALESCE(SUM(v.total_dwell_s), 0)::float  AS total_dwell_s,
               COALESCE(AVG(v.total_dwell_s), 0)::float  AS avg_dwell_s
        FROM session_zone_visits v
        JOIN sessions s ON s.session_id = v.session_id
        WHERE s.entered_at >= now() - (%s * interval '1 hour')
          AND s.role != 'staff'
        GROUP BY v.zone_id
        ORDER BY unique_visitors DESC
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (hours,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_session(pool: AsyncConnectionPool, session_id: str) -> dict[str, Any] | None:
    sql = "SELECT * FROM sessions WHERE session_id = %s"
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (session_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def fetch_cameras_health(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    sql = """
        SELECT camera_id,
               COUNT(*)            AS events_last_5_min,
               MAX(ts)             AS last_event_at
        FROM raw_events
        WHERE ts >= now() - interval '5 minutes' AND camera_id IS NOT NULL
        GROUP BY camera_id
        ORDER BY camera_id
    """
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Hourly rollup (called periodically by aggregator)
# --------------------------------------------------------------------------


REFRESH_HOURLY_SQL = """
    INSERT INTO hourly_metrics (
        hour_bucket, footfall, unique_visitors, purchases, checkouts,
        conversion_rate, avg_session_duration_s, refreshed_at
    )
    SELECT
        date_trunc('hour', entered_at) AS hour_bucket,
        COUNT(*)                       AS footfall,
        COUNT(DISTINCT COALESCE(embedding_id, session_id::text)) AS unique_visitors,
        COUNT(*) FILTER (WHERE funnel_stage = 'purchased')         AS purchases,
        COUNT(*) FILTER (WHERE funnel_stage IN ('checkout_queued','purchased')) AS checkouts,
        CASE WHEN COUNT(*) > 0 THEN
            (COUNT(*) FILTER (WHERE funnel_stage = 'purchased'))::numeric / COUNT(*)
        ELSE 0 END                     AS conversion_rate,
        AVG(EXTRACT(EPOCH FROM (exited_at - entered_at))) AS avg_session_duration_s,
        now()
    FROM sessions
    WHERE entered_at >= now() - interval '48 hours'
      AND role != 'staff'
    GROUP BY hour_bucket
    ON CONFLICT (hour_bucket) DO UPDATE SET
        footfall               = EXCLUDED.footfall,
        unique_visitors        = EXCLUDED.unique_visitors,
        purchases              = EXCLUDED.purchases,
        checkouts              = EXCLUDED.checkouts,
        conversion_rate        = EXCLUDED.conversion_rate,
        avg_session_duration_s = EXCLUDED.avg_session_duration_s,
        refreshed_at           = EXCLUDED.refreshed_at
"""


async def refresh_hourly_metrics(pool: AsyncConnectionPool) -> int:
    """Recompute the last 48h of hourly_metrics. Returns rows affected."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(REFRESH_HOURLY_SQL)
        return cur.rowcount or 0


async def wait_for_db(url: str, timeout_s: int = 60) -> None:
    """Poll Postgres on startup so the aggregator survives image bring-up."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(
                url.replace("postgresql+psycopg://", "postgresql://", 1)
            )
            await conn.close()
            return
        except Exception:  # noqa: BLE001
            if asyncio.get_event_loop().time() > deadline:
                raise
            await asyncio.sleep(1)
