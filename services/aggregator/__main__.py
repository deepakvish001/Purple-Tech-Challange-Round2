"""Aggregator entrypoint.

Consumes events from Redis Streams, applies them to the in-memory
SessionStore, persists deltas to Postgres, and periodically refreshes
hourly_metrics + runs anomaly detection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import UTC, datetime

import structlog

from services.aggregator import anomalies, db, seed
from services.aggregator.handlers import persist_delta
from services.aggregator.session import SessionStore
from services.events import EventBus

log = structlog.get_logger()

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://store:store@postgres:5432/store"
)
CONSUMER_GROUP = "aggregator"
CONSUMER_NAME  = os.environ.get("AGG_CONSUMER_NAME", "agg-1")
ROLLUP_INTERVAL_S    = int(os.environ.get("ROLLUP_INTERVAL_S", "30"))
ANOMALY_INTERVAL_S   = int(os.environ.get("ANOMALY_INTERVAL_S", "60"))


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s", stream=sys.stdout)
    structlog.configure(processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])


async def consume_loop(bus: EventBus, pool, store: SessionStore, stop: asyncio.Event) -> None:
    log.info("consume.start", group=CONSUMER_GROUP, name=CONSUMER_NAME)
    await bus.ensure_group(CONSUMER_GROUP, start="0")
    n = 0
    async for stream_id, env in bus.consume(CONSUMER_GROUP, CONSUMER_NAME):
        if stop.is_set():
            break
        delta = store.apply(env)
        await persist_delta(pool, env, delta)
        await bus.ack(CONSUMER_GROUP, stream_id)
        n += 1
        if n % 50 == 0:
            log.info("consume.progress", events=n, open_sessions=store.open_count)


async def periodic(name: str, interval_s: int, fn, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("%s.tick_failed", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass


async def _main() -> None:
    _setup_logging()
    log.info("agg.boot", redis=REDIS_URL, db=DATABASE_URL)

    # Wait for Postgres to be reachable.
    await db.wait_for_db(DATABASE_URL, timeout_s=120)
    pool = await db.open_pool(DATABASE_URL)

    # First boot: populate the dashboard with a realistic 24h shift so the
    # reviewer sees a story, not an empty page. No-op on subsequent restarts.
    if os.environ.get("SEED_DEMO_ON_BOOT", "true").lower() == "true":
        try:
            n = await seed.seed_if_empty(pool)
            if n:
                await db.refresh_hourly_metrics(pool)
                log.info("agg.seed_demo", sessions=n)
        except Exception:  # noqa: BLE001
            log.exception("agg.seed_failed")
    bus = EventBus(REDIS_URL)
    # Wait for Redis too.
    for _ in range(15):
        try:
            await bus.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)

    store = SessionStore()
    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        log.info("agg.signal_shutdown")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    async def _rollup() -> None:
        rc = await db.refresh_hourly_metrics(pool)
        log.info("rollup", rows=rc, ts=datetime.now(UTC).isoformat())

    async def _anomalies() -> None:
        out = await anomalies.run_all(pool)
        if any(v for v in out.values()):
            log.info("anomalies.inserted", **out)

    tasks = [
        asyncio.create_task(consume_loop(bus, pool, store, stop), name="consume"),
        asyncio.create_task(periodic("rollup",   ROLLUP_INTERVAL_S,  _rollup,   stop), name="rollup"),
        asyncio.create_task(periodic("anomaly",  ANOMALY_INTERVAL_S, _anomalies, stop), name="anomaly"),
    ]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await pool.close()
    await bus.close()
    log.info("agg.shutdown_complete")


if __name__ == "__main__":
    asyncio.run(_main())
