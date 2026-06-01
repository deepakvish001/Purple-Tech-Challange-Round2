"""Glue: take a SessionDelta from the state machine, persist it to Postgres."""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from services.aggregator import db
from services.aggregator.session import Session, SessionDelta
from services.events.schemas import Envelope

log = logging.getLogger(__name__)


async def persist_delta(pool: AsyncConnectionPool, env: Envelope, delta: SessionDelta) -> None:
    """Write the raw event + session mutations + any new zone visits."""
    session_id_for_raw = delta.session.session_id if delta.action != "noop" else None
    try:
        await db.insert_raw_event(pool, env, session_id_for_raw)
    except Exception:
        log.exception("raw_event insert failed event_id=%s", env.event_id)

    if delta.action == "noop":
        return

    s: Session = delta.session
    try:
        await db.upsert_session(
            pool,
            session_id=s.session_id,
            store_id=s.store_id,
            embedding_id=s.embedding_id,
            role=s.role,
            entered_at=s.entered_at,
            exited_at=s.exited_at,
            funnel_stage=s.funnel_stage,
            checkout_at=s.checkout_at,
            receipt=s.receipt,
        )
    except Exception:
        log.exception("session upsert failed sid=%s", s.session_id)
        return

    # Only persist zones touched by this event (cheap heuristic: persist
    # all of them — small, bounded). Skips when the env didn't carry one.
    for zone_id, zv in s.zones.items():
        try:
            await db.upsert_zone_visit(
                pool,
                session_id=s.session_id,
                zone_id=zone_id,
                first_seen=zv.first_seen,
                last_seen=zv.last_seen,
                total_dwell_s=zv.total_dwell_s,
            )
        except Exception:
            log.exception("zone_visit upsert failed sid=%s zone=%s", s.session_id, zone_id)
