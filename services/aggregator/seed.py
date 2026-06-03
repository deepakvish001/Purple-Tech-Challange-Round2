"""Seed 24 hours of demo data on first boot.

Without this, a freshly-started stack shows ~ 60 seconds of synth events
on the dashboard — useless for a demo. With it, the reviewer opens the
dashboard and immediately sees a realistic shift (480 sessions, footfall
peaks at lunch + evening, a populated funnel, sales, anomalies).

Runs once: `seed_if_empty` is a no-op when the `sessions` table already
has rows, so subsequent restarts keep history. `docker compose down -v`
wipes the volume and reseeds on the next `up`.

Pure data generation lives in `generate_demo_sessions` and is
unit-tested without a database.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Static catalogue — mirrors config/zones/cam{1,2}.yaml.
# Weight = relative popularity (drives both pick probability and dwell time).
# --------------------------------------------------------------------------

SHELVES: list[tuple[str, float]] = [
    ("shelf_lakme",         1.00),
    ("shelf_maybelline",    0.85),
    ("shelf_face_shop",     0.55),
    ("shelf_aqualogica",    0.50),
    ("shelf_minimalist",    0.45),
    ("shelf_loreal",        0.55),
    ("shelf_swiss_beauty",  0.40),
    ("shelf_farmstay",      0.40),
    ("shelf_good_vibes",    0.30),
    ("shelf_dermdoc",       0.30),
    ("shelf_ny_bae",        0.40),
    ("shelf_faces_canada",  0.35),
    ("shelf_alps",          0.30),
    ("shelf_accessories",   0.50),
]

PAYMENT_MODES = ["UPI", "UPI", "UPI", "UPI", "CARD", "CARD", "CASH"]
SALESPEOPLE   = ["971", "1178", "523", "1190", "737"]

# Brigade Road footfall pattern. Store is open 11:00 - 22:00; lunch peak
# 13-14:00, evening peak 19-21:00. Weights are relative.
HOUR_WEIGHTS: dict[int, float] = {
    11: 0.40, 12: 0.65, 13: 1.00, 14: 0.80,
    15: 0.55, 16: 0.60, 17: 0.75, 18: 0.90,
    19: 1.00, 20: 1.00, 21: 0.80, 22: 0.35,
}


# --------------------------------------------------------------------------
# Pure data generation (testable without a DB)
# --------------------------------------------------------------------------


@dataclass
class _ZoneVisit:
    zone_id: str
    first_seen: datetime
    last_seen: datetime
    total_dwell_s: float


@dataclass
class _Session:
    session_id: str
    store_id: str
    embedding_id: str
    role: str                       # "customer" | "staff"
    entered_at: datetime
    exited_at: datetime
    funnel_stage: str               # entered / browsed / engaged / checkout_queued / purchased
    checkout_at: datetime | None
    receipt: dict[str, Any] | None
    zone_visits: list[_ZoneVisit]


def _pick_stage(rng: random.Random) -> str:
    """Funnel distribution tuned for a healthy cosmetics-store shift."""
    r = rng.random()
    if r < 0.186:
        return "purchased"
    if r < 0.290:
        return "checkout_queued"
    if r < 0.530:
        return "engaged"
    if r < 0.800:
        return "browsed"
    return "entered"


def _make_zone_visits(
    rng: random.Random, stage: str, entered_at: datetime
) -> list[_ZoneVisit]:
    if stage == "entered":
        return []
    n_zones = rng.choices([1, 2, 2, 3, 3, 4], weights=[20, 25, 20, 15, 12, 8])[0]
    ids = [z for z, _ in SHELVES]
    weights = [w for _, w in SHELVES]
    chosen: list[str] = []
    while len(chosen) < n_zones:
        z = rng.choices(ids, weights=weights)[0]
        if z not in chosen:
            chosen.append(z)
    cursor = entered_at + timedelta(seconds=rng.randint(5, 25))
    out: list[_ZoneVisit] = []
    for z in chosen:
        dwell_lo, dwell_hi = (15, 90) if stage in ("engaged", "checkout_queued", "purchased") else (3, 18)
        dwell = round(rng.uniform(dwell_lo, dwell_hi), 1)
        out.append(_ZoneVisit(z, cursor, cursor + timedelta(seconds=dwell), dwell))
        cursor += timedelta(seconds=dwell + rng.randint(3, 12))
    return out


def _make_receipt(rng: random.Random) -> dict[str, Any]:
    items = rng.choices([1, 1, 2, 2, 3, 4, 5, 6], weights=[22, 18, 18, 14, 10, 8, 6, 4])[0]
    total = round(items * rng.uniform(180, 850), 2)
    return {
        "invoice_number": f"ML0426KAP{rng.randint(1_000_000, 9_999_999)}",
        "total_amount":   total,
        "item_count":     items,
        "payment_mode":   rng.choice(PAYMENT_MODES),
        "salesperson_id": rng.choice(SALESPEOPLE),
    }


def _make_session(rng: random.Random, store_id: str, entered_at: datetime) -> _Session:
    sid = str(uuid4())
    duration_s = max(45, int(rng.gauss(290, 130)))
    exited_at = entered_at + timedelta(seconds=duration_s)
    stage = _pick_stage(rng)
    role = "staff" if rng.random() < 0.04 else "customer"
    checkout_at = None
    receipt = None
    if stage in ("checkout_queued", "purchased"):
        checkout_at = entered_at + timedelta(seconds=max(30, duration_s - rng.randint(20, 90)))
    if stage == "purchased":
        receipt = _make_receipt(rng)
    return _Session(
        session_id   = sid,
        store_id     = store_id,
        embedding_id = f"emb_seed_{sid[:8]}",
        role         = role,
        entered_at   = entered_at,
        exited_at    = exited_at,
        funnel_stage = stage,
        checkout_at  = checkout_at,
        receipt      = receipt,
        zone_visits  = _make_zone_visits(rng, stage, entered_at),
    )


def generate_demo_sessions(
    n: int = 480, store_id: str = "ST1008", seed: int = 20260603,
    now: datetime | None = None,
) -> list[_Session]:
    """Generate `n` realistic sessions over the trailing 24 hours."""
    rng = random.Random(seed)
    now = now or datetime.now(UTC)
    hour_buckets: list[tuple[int, float]] = []
    for h_back in range(24):
        local_hour = (now - timedelta(hours=h_back)).hour
        w = HOUR_WEIGHTS.get(local_hour, 0.0)
        if w > 0:
            hour_buckets.append((h_back, w))
    if not hour_buckets:
        return []
    sessions: list[_Session] = []
    weights = [w for _, w in hour_buckets]
    for _ in range(n):
        h_back, _ = rng.choices(hour_buckets, weights=weights)[0]
        minutes = rng.uniform(0, 60)
        entered_at = now - timedelta(hours=h_back) + timedelta(minutes=minutes)
        if entered_at > now:
            entered_at = now - timedelta(seconds=rng.randint(60, 600))
        sessions.append(_make_session(rng, store_id, entered_at))
    sessions.sort(key=lambda s: s.entered_at)
    return sessions


def make_anomalies(now: datetime | None = None) -> list[dict[str, Any]]:
    """Two seed anomalies so the feature is demoable from minute one."""
    now = now or datetime.now(UTC)
    return [
        {
            "kind":      "footfall_outlier",
            "severity":  "warning",
            "bucket_ts": (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0),
            "details": {
                "footfall":        89,
                "baseline_mean":   31.2,
                "baseline_stddev": 8.4,
                "z_score":         6.88,
            },
        },
        {
            "kind":      "dead_zone",
            "severity":  "info",
            "bucket_ts": now.replace(minute=0, second=0, microsecond=0),
            "details": {
                "zone_id":            "shelf_swiss_beauty",
                "visitors_last_hour": 2,
                "baseline_median":    18.5,
            },
        },
    ]


# --------------------------------------------------------------------------
# Postgres writers
# --------------------------------------------------------------------------


async def is_empty(pool: AsyncConnectionPool) -> bool:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM sessions")
        row = await cur.fetchone()
    return (row[0] if row else 0) == 0


async def seed(pool: AsyncConnectionPool, n: int = 480) -> int:
    sessions = generate_demo_sessions(n=n)
    async with pool.connection() as conn, conn.cursor() as cur:
        for s in sessions:
            r = s.receipt or {}
            await cur.execute(
                """INSERT INTO sessions (
                    session_id, store_id, embedding_id, role, entered_at, exited_at,
                    funnel_stage, checkout_at,
                    receipt_invoice, receipt_total, receipt_items, receipt_mode, receipt_salesperson
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    s.session_id, s.store_id, s.embedding_id, s.role,
                    s.entered_at, s.exited_at, s.funnel_stage, s.checkout_at,
                    r.get("invoice_number"), r.get("total_amount"), r.get("item_count"),
                    r.get("payment_mode"),  r.get("salesperson_id"),
                ),
            )
            for v in s.zone_visits:
                await cur.execute(
                    """INSERT INTO session_zone_visits
                        (session_id, zone_id, first_seen, last_seen, total_dwell_s)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (s.session_id, v.zone_id, v.first_seen, v.last_seen, v.total_dwell_s),
                )
        for a in make_anomalies():
            await cur.execute(
                """INSERT INTO anomalies (kind, severity, bucket_ts, details)
                   VALUES (%s,%s,%s,%s::jsonb)""",
                (a["kind"], a["severity"], a["bucket_ts"], json.dumps(a["details"])),
            )
    return len(sessions)


async def seed_if_empty(pool: AsyncConnectionPool) -> int:
    if await is_empty(pool):
        n = await seed(pool)
        log.info("demo seed complete sessions=%d", n)
        return n
    return 0
