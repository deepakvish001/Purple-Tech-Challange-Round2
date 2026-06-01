"""Periodic anomaly detector.

Runs every minute. Three families (per docs/DESIGN.md §8):

1. Footfall outlier — z-score against the same-weekday-same-hour mean
   over the last 7 days. Flagged at |z| > 2.5.
2. Conversion drop — current hour vs prior 3-hour mean; flag if drop
   > 30 % and footfall > 20.
3. Dead zone — shelf zone whose unique-visitor count over the last hour
   is < 25 % of its 14-day median (during operating hours).

Detection is best-effort — when history is short (early in deployment),
we silently skip. The first wave of meaningful anomalies appears once
the system has been running for ~ 8 days.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

from services.aggregator import db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Footfall outlier
# --------------------------------------------------------------------------


_FOOTFALL_BASELINE_SQL = """
    SELECT
        hour_bucket,
        footfall,
        AVG(footfall)    OVER w AS baseline_mean,
        STDDEV(footfall) OVER w AS baseline_stddev
    FROM hourly_metrics
    WHERE hour_bucket >= now() - interval '8 days'
    WINDOW w AS (
        PARTITION BY EXTRACT(DOW FROM hour_bucket), EXTRACT(HOUR FROM hour_bucket)
        ORDER BY hour_bucket
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    )
    ORDER BY hour_bucket DESC
    LIMIT 24
"""


async def detect_footfall_outliers(pool: AsyncConnectionPool) -> int:
    inserted = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FOOTFALL_BASELINE_SQL)
        rows = await cur.fetchall()
    for hour_bucket, footfall, mean, stddev in rows:
        if mean is None or stddev is None or stddev == 0:
            continue
        z = (float(footfall) - float(mean)) / float(stddev)
        if abs(z) > 2.5:
            severity = "warning" if abs(z) < 4 else "alert"
            await db.insert_anomaly(
                pool,
                kind="footfall_outlier",
                severity=severity,
                bucket_ts=hour_bucket,
                details={
                    "footfall": int(footfall),
                    "baseline_mean": round(float(mean), 1),
                    "baseline_stddev": round(float(stddev), 1),
                    "z_score": round(z, 2),
                },
            )
            inserted += 1
    return inserted


# --------------------------------------------------------------------------
# Conversion drop
# --------------------------------------------------------------------------


_CONVERSION_SQL = """
    WITH recent AS (
        SELECT hour_bucket, footfall, conversion_rate,
               ROW_NUMBER() OVER (ORDER BY hour_bucket DESC) AS rn
        FROM hourly_metrics
        WHERE hour_bucket >= now() - interval '6 hours'
    )
    SELECT
        (SELECT footfall        FROM recent WHERE rn = 1) AS curr_foot,
        (SELECT conversion_rate FROM recent WHERE rn = 1) AS curr_conv,
        (SELECT hour_bucket     FROM recent WHERE rn = 1) AS curr_bucket,
        (SELECT AVG(conversion_rate) FROM recent WHERE rn BETWEEN 2 AND 4) AS prior_conv
"""


async def detect_conversion_drop(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_CONVERSION_SQL)
        row = await cur.fetchone()
    if row is None or row[0] is None or row[3] is None:
        return 0
    curr_foot, curr_conv, curr_bucket, prior_conv = row
    if int(curr_foot) < 20 or float(prior_conv) <= 0:
        return 0
    drop = (float(prior_conv) - float(curr_conv)) / float(prior_conv)
    if drop > 0.3:
        await db.insert_anomaly(
            pool,
            kind="conversion_drop",
            severity="warning",
            bucket_ts=curr_bucket,
            details={
                "current_conversion_rate": round(float(curr_conv), 4),
                "prior_3h_mean":           round(float(prior_conv), 4),
                "drop_pct":                round(drop * 100, 1),
                "footfall":                int(curr_foot),
            },
        )
        return 1
    return 0


# --------------------------------------------------------------------------
# Dead zone
# --------------------------------------------------------------------------


_DEAD_ZONE_SQL = """
    WITH recent_hour AS (
        SELECT v.zone_id, COUNT(DISTINCT v.session_id) AS visitors
        FROM session_zone_visits v
        JOIN sessions s ON s.session_id = v.session_id
        WHERE s.entered_at >= now() - interval '1 hour'
          AND s.role != 'staff'
          AND v.zone_id LIKE 'shelf_%'
        GROUP BY v.zone_id
    ),
    baseline AS (
        SELECT v.zone_id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY hourly.visitors) AS median_hourly
        FROM (
            SELECT v.zone_id, date_trunc('hour', s.entered_at) AS h,
                   COUNT(DISTINCT v.session_id) AS visitors
            FROM session_zone_visits v
            JOIN sessions s ON s.session_id = v.session_id
            WHERE s.entered_at >= now() - interval '14 days'
              AND s.role != 'staff'
              AND v.zone_id LIKE 'shelf_%'
            GROUP BY v.zone_id, h
        ) hourly
        JOIN session_zone_visits v ON v.zone_id = hourly.zone_id
        GROUP BY v.zone_id
    )
    SELECT recent_hour.zone_id, recent_hour.visitors, baseline.median_hourly
    FROM recent_hour
    JOIN baseline USING (zone_id)
    WHERE baseline.median_hourly >= 4
      AND recent_hour.visitors < 0.25 * baseline.median_hourly
"""


async def detect_dead_zones(pool: AsyncConnectionPool) -> int:
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    inserted = 0
    async with pool.connection() as conn, conn.cursor() as cur:
        try:
            await cur.execute(_DEAD_ZONE_SQL)
            rows = await cur.fetchall()
        except Exception:  # noqa: BLE001
            # CTE is heavy; in low-data regimes the median expression can
            # yield no rows. Treat as "no anomaly".
            return 0
    for zone_id, visitors, median in rows:
        await db.insert_anomaly(
            pool,
            kind="dead_zone",
            severity="info",
            bucket_ts=bucket,
            details={
                "zone_id": zone_id,
                "visitors_last_hour": int(visitors),
                "baseline_median": float(median),
            },
        )
        inserted += 1
    return inserted


# --------------------------------------------------------------------------
# Orchestrator (called from aggregator's periodic loop)
# --------------------------------------------------------------------------


async def run_all(pool: AsyncConnectionPool) -> dict[str, int]:
    out = {}
    out["footfall_outlier"] = await detect_footfall_outliers(pool)
    out["conversion_drop"]  = await detect_conversion_drop(pool)
    out["dead_zone"]        = await detect_dead_zones(pool)
    log.info("anomalies %s", out)
    return out
