"""Synthetic event publisher.

Generates a realistic stream of detection events without needing the
680 MB of CCTV footage. Drives `docker compose up` to a verifiable state
within seconds, which is what survives the acceptance gate.

The simulator models ~30 customer sessions over a configurable simulated
duration. Each session: enter via CAM 3 → browse 1–3 shelf zones on CAM 1
or CAM 2 → maybe checkout on CAM 5 → exit via CAM 3. Two staff members
periodically appear on CAM 4. POS receipts are emitted for most checkout
events with a small offset.

Real-time pacing is controlled by `SYNTH_PACE` (1.0 = wall-clock,
0 = as-fast-as-possible — used in tests).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from services.events import (
    Envelope,
    EventBus,
    checkout_observed,
    person_entered,
    person_exited,
    pos_receipt,
    staff_observed,
    zone_dwell,
    zone_entered,
)
from services.ingest.config import load_store_cfg

log = structlog.get_logger()

# --------------------------------------------------------------------------
# Defaults — overridable via env in __main__.
# --------------------------------------------------------------------------

DEFAULTS = {
    "STORE_ID": "ST1008",
    "SYNTH_SEED": "42",
    "SYNTH_SESSIONS": "30",
    "SYNTH_DURATION_S": "600",   # 10 sim-minutes
    "SYNTH_PACE": "1.0",
    "REDIS_URL": "redis://redis:6379/0",
}


@dataclass
class SynthConfig:
    store_id: str
    seed: int
    sessions: int
    duration_s: int
    pace: float
    shelf_zones_cam1: list[str]
    shelf_zones_cam2: list[str]


def synth_config_from_env() -> SynthConfig:
    def _g(k: str) -> str:
        return os.environ.get(k, DEFAULTS[k])

    # Pull zone IDs out of the loaded store config so synthetic events use
    # the same ids the future aggregator expects.
    try:
        cfg = load_store_cfg()
        cam1_zones = [z.id for z in cfg.by_id("cam_1_top").zones if z.id.startswith("shelf_")]
        cam2_zones = [z.id for z in cfg.by_id("cam_2_bottom").zones if z.id.startswith("shelf_")]
    except Exception:  # noqa: BLE001
        # In tests run outside the container we may not have CONFIG_DIR.
        cam1_zones = ["shelf_farmstay", "shelf_face_shop", "shelf_good_vibes"]
        cam2_zones = ["shelf_maybelline", "shelf_lakme", "shelf_swiss_beauty"]

    return SynthConfig(
        store_id=_g("STORE_ID"),
        seed=int(_g("SYNTH_SEED")),
        sessions=int(_g("SYNTH_SESSIONS")),
        duration_s=int(_g("SYNTH_DURATION_S")),
        pace=float(_g("SYNTH_PACE")),
        shelf_zones_cam1=cam1_zones,
        shelf_zones_cam2=cam2_zones,
    )


# --------------------------------------------------------------------------
# Pure timeline generator (no I/O — testable).
# --------------------------------------------------------------------------


def _staff_tracks(rng: random.Random, base: datetime, duration_s: int) -> Iterable[Envelope]:
    """Two staff members ping CAM 4 every 1–3 min."""
    for staff_idx in (1, 2):
        track = f"c4_staff_{staff_idx}"
        t = base + timedelta(seconds=rng.uniform(0, 60))
        while t < base + timedelta(seconds=duration_s):
            yield staff_observed(
                store_id="_",  # filled per-session below by caller
                camera_id="cam_4_boh",
                track_id=track,
                embedding_id=f"emb_staff_{staff_idx}",
                ts=t,
            )
            t += timedelta(seconds=rng.uniform(60, 180))


def _session_events(
    rng: random.Random,
    session_idx: int,
    enter_ts: datetime,
    cfg: SynthConfig,
) -> list[Envelope]:
    """All detection events for one customer session, in time order."""
    embedding_id = f"emb_cust_{session_idx:04d}"
    cam3_track = f"c3_track_{session_idx}"
    enter = person_entered(
        store_id=cfg.store_id,
        camera_id="cam_3_entry",
        line_id="door_main",
        track_id=cam3_track,
        embedding_id=embedding_id,
        ts=enter_ts,
        role="customer",
    )

    events: list[Envelope] = [enter]

    # 1–3 shelf visits, mixed across the two F.O.H cameras.
    n_zones = rng.choice([1, 1, 2, 2, 3])
    cam1 = cfg.shelf_zones_cam1 or ["shelf_farmstay"]
    cam2 = cfg.shelf_zones_cam2 or ["shelf_maybelline"]
    cursor = enter_ts + timedelta(seconds=rng.uniform(5, 15))  # door → shelf walk
    for v in range(n_zones):
        use_cam2 = rng.random() < 0.5
        camera_id = "cam_2_bottom" if use_cam2 else "cam_1_top"
        track = f"c{2 if use_cam2 else 1}_track_{session_idx}_{v}"
        zone_id = rng.choice(cam2 if use_cam2 else cam1)
        events.append(
            zone_entered(
                store_id=cfg.store_id,
                camera_id=camera_id,
                zone_id=zone_id,
                track_id=track,
                first_visit_in_session=(v == 0),
                embedding_id=embedding_id,
                ts=cursor,
                role="customer",
            )
        )
        dwell = rng.uniform(8, 75)  # most browsers move on, some engage
        cursor += timedelta(seconds=dwell)
        events.append(
            zone_dwell(
                store_id=cfg.store_id,
                camera_id=camera_id,
                zone_id=zone_id,
                dwell_s=round(dwell, 1),
                track_id=track,
                embedding_id=embedding_id,
                ts=cursor,
                role="customer",
            )
        )
        cursor += timedelta(seconds=rng.uniform(2, 6))  # walk to next shelf

    # ~ 55 % of sessions reach checkout, ~ 40 % of those purchase.
    reaches_checkout = rng.random() < 0.55
    purchased = False
    if reaches_checkout:
        cursor += timedelta(seconds=rng.uniform(4, 10))
        cam5_track = f"c5_track_{session_idx}"
        events.append(
            checkout_observed(
                store_id=cfg.store_id,
                camera_id="cam_5_cash",
                zone_id="cash_counter",
                track_id=cam5_track,
                queue_position=rng.choice([0, 0, 1, 1, 2]),
                embedding_id=embedding_id,
                ts=cursor,
                role="customer",
            )
        )
        purchased = rng.random() < 0.4
        if purchased:
            receipt_offset = rng.uniform(20, 70)
            events.append(
                pos_receipt(
                    store_id=cfg.store_id,
                    invoice_number=f"ML0426KAP{session_idx:07d}",
                    salesperson_id=rng.choice(["971", "1178", "523", "1190", "737"]),
                    total_amount=round(rng.uniform(99, 4500), 2),
                    item_count=rng.randint(1, 6),
                    payment_mode=rng.choice(["UPI", "CARD", "CASH"]),
                    ts=cursor + timedelta(seconds=receipt_offset),
                )
            )
            cursor += timedelta(seconds=receipt_offset + rng.uniform(5, 20))
        else:
            cursor += timedelta(seconds=rng.uniform(10, 40))  # browsed then left

    # Exit via CAM 3.
    exit_ts = cursor + timedelta(seconds=rng.uniform(5, 15))
    session_duration = (exit_ts - enter_ts).total_seconds()
    events.append(
        person_exited(
            store_id=cfg.store_id,
            camera_id="cam_3_entry",
            line_id="door_main",
            track_id=cam3_track,
            session_duration_s=round(session_duration, 1),
            embedding_id=embedding_id,
            ts=exit_ts,
            role="customer",
        )
    )
    return events


def generate_timeline(cfg: SynthConfig, *, base: datetime | None = None) -> list[Envelope]:
    """Build the entire timeline of events in time order. Pure / testable."""
    rng = random.Random(cfg.seed)
    base = base or datetime.now(UTC)
    events: list[Envelope] = []

    # Customer sessions spaced uniformly across `duration_s`.
    for i in range(cfg.sessions):
        enter_offset = (i + rng.uniform(0, 1)) * cfg.duration_s / cfg.sessions
        enter_ts = base + timedelta(seconds=enter_offset)
        events.extend(_session_events(rng, i, enter_ts, cfg))

    # Staff pings on CAM 4.
    for ev in _staff_tracks(rng, base, cfg.duration_s):
        # _staff_tracks emits with placeholder store; fill in the real one.
        events.append(
            staff_observed(
                store_id=cfg.store_id,
                camera_id=ev.camera_id or "cam_4_boh",
                track_id=ev.track_id or "c4_staff_x",
                embedding_id=ev.embedding_id,
                ts=ev.ts,
            )
        )

    events.sort(key=lambda e: e.ts)
    return events


# --------------------------------------------------------------------------
# Async publisher — replays the timeline to a real Redis at configurable
# pace. `pace=0` publishes everything as fast as possible.
# --------------------------------------------------------------------------


async def replay(bus: EventBus, events: list[Envelope], pace: float) -> AsyncIterator[Envelope]:
    """Publish events in time order, sleeping between them per `pace`."""
    if not events:
        return
    sim_start = events[0].ts
    wall_start = datetime.now(UTC)
    for ev in events:
        if pace > 0:
            target_offset = (ev.ts - sim_start).total_seconds() / pace
            wall_elapsed = (datetime.now(UTC) - wall_start).total_seconds()
            sleep_s = target_offset - wall_elapsed
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
        # Re-stamp ts to "now" so downstream consumers see live wall-clock
        # events; the relative ordering is preserved by the sleep above.
        ev = ev.model_copy(update={"ts": datetime.now(UTC)})
        await bus.publish(ev)
        yield ev


async def run(bus: EventBus, cfg: SynthConfig) -> None:
    log.info("synth.start", sessions=cfg.sessions, duration_s=cfg.duration_s, pace=cfg.pace)
    events = generate_timeline(cfg)
    log.info("synth.timeline_built", events=len(events))
    published = 0
    async for _ in replay(bus, events, cfg.pace):
        published += 1
        if published % 25 == 0:
            log.info("synth.progress", published=published, total=len(events))
    log.info("synth.complete", published=published)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
    )


async def _main() -> None:
    _setup_logging()
    cfg = synth_config_from_env()
    bus = EventBus(os.environ.get("REDIS_URL", DEFAULTS["REDIS_URL"]))

    # Retry ping briefly while Redis starts.
    for attempt in range(15):
        try:
            await bus.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)
            if attempt == 14:
                raise

    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        log.info("synth.signal_shutdown")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    runner = asyncio.create_task(run(bus, cfg))
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    if runner in done:
        # natural completion — loop forever publishing fresh timelines so
        # the dashboard always has something to display
        while not stop.is_set():
            log.info("synth.loop_restart")
            await run(bus, synth_config_from_env())
    else:
        runner.cancel()

    await bus.close()


if __name__ == "__main__":
    asyncio.run(_main())
