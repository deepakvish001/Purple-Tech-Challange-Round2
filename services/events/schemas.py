"""Pydantic event schemas.

The on-the-wire JSON shape is defined in docs/EVENT_SCHEMA.md. This module
is the single source of truth; payload validation per `type` is enforced by
the constructor helpers at the bottom.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventType = Literal[
    "person_entered",
    "person_exited",
    "zone_entered",
    "zone_dwell",
    "checkout_observed",
    "staff_observed",
    "pos_receipt",
    "health_warning",
]

EVENT_TYPES: tuple[str, ...] = get_args(EventType)

Role = Literal["customer", "staff", "unknown"]


class Envelope(BaseModel):
    """Common envelope for every event on the bus."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    type: EventType
    store_id: str
    camera_id: str | None = None
    ts: datetime
    session_id: str | None = None
    track_id: str | None = None
    embedding_id: str | None = None
    role: Role = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        return v.astimezone(UTC)

    def to_json(self) -> str:
        # mode=json renders datetime as RFC 3339 string.
        return self.model_dump_json()

    @classmethod
    def from_json(cls, s: str | bytes) -> Envelope:
        return cls.model_validate_json(s)


# Re-export under the more readable name used elsewhere.
Event = Envelope


# --------------------------------------------------------------------------
# Per-type payload validators. These exist so the constructor helpers below
# fail loudly when callers pass the wrong shape; the on-the-wire envelope
# still carries a plain dict to keep consumers simple.
# --------------------------------------------------------------------------


class _PersonCrossing(BaseModel):
    direction: Literal["in", "out"]
    line_id: str
    session_duration_s: float | None = None


class _ZoneEntered(BaseModel):
    zone_id: str
    first_visit_in_session: bool = False


class _ZoneDwell(BaseModel):
    zone_id: str
    dwell_s: float


class _Checkout(BaseModel):
    zone_id: str
    queue_position: int = 0


class _PosReceipt(BaseModel):
    invoice_number: str
    salesperson_id: str | None = None
    total_amount: float
    item_count: int
    payment_mode: str | None = None


class _HealthWarning(BaseModel):
    source: str
    reason: str


# --------------------------------------------------------------------------
# Constructor helpers. Use these from publishers — they validate the payload
# shape against the type and stamp ts in UTC if not supplied.
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _env(
    type_: EventType,
    *,
    store_id: str,
    camera_id: str | None,
    ts: datetime | None,
    session_id: str | None,
    track_id: str | None,
    embedding_id: str | None,
    role: Role,
    payload: BaseModel,
) -> Envelope:
    return Envelope(
        type=type_,
        store_id=store_id,
        camera_id=camera_id,
        ts=ts or _now(),
        session_id=session_id,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=payload.model_dump(exclude_none=True),
    )


def person_entered(
    *,
    store_id: str,
    camera_id: str,
    line_id: str,
    track_id: str,
    embedding_id: str | None = None,
    ts: datetime | None = None,
    role: Role = "unknown",
) -> Envelope:
    return _env(
        "person_entered",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts,
        session_id=None,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=_PersonCrossing(direction="in", line_id=line_id),
    )


def person_exited(
    *,
    store_id: str,
    camera_id: str,
    line_id: str,
    track_id: str,
    session_duration_s: float | None = None,
    embedding_id: str | None = None,
    ts: datetime | None = None,
    role: Role = "unknown",
) -> Envelope:
    return _env(
        "person_exited",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts,
        session_id=None,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=_PersonCrossing(direction="out", line_id=line_id, session_duration_s=session_duration_s),
    )


def zone_entered(
    *,
    store_id: str,
    camera_id: str,
    zone_id: str,
    track_id: str,
    first_visit_in_session: bool = False,
    embedding_id: str | None = None,
    ts: datetime | None = None,
    role: Role = "unknown",
) -> Envelope:
    return _env(
        "zone_entered",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts,
        session_id=None,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=_ZoneEntered(zone_id=zone_id, first_visit_in_session=first_visit_in_session),
    )


def zone_dwell(
    *,
    store_id: str,
    camera_id: str,
    zone_id: str,
    dwell_s: float,
    track_id: str,
    embedding_id: str | None = None,
    ts: datetime | None = None,
    role: Role = "unknown",
) -> Envelope:
    return _env(
        "zone_dwell",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts,
        session_id=None,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=_ZoneDwell(zone_id=zone_id, dwell_s=dwell_s),
    )


def checkout_observed(
    *,
    store_id: str,
    camera_id: str,
    zone_id: str,
    track_id: str,
    queue_position: int = 0,
    embedding_id: str | None = None,
    ts: datetime | None = None,
    role: Role = "unknown",
) -> Envelope:
    return _env(
        "checkout_observed",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts,
        session_id=None,
        track_id=track_id,
        embedding_id=embedding_id,
        role=role,
        payload=_Checkout(zone_id=zone_id, queue_position=queue_position),
    )


def staff_observed(
    *,
    store_id: str,
    camera_id: str,
    track_id: str,
    embedding_id: str | None = None,
    ts: datetime | None = None,
) -> Envelope:
    return Envelope(
        type="staff_observed",
        store_id=store_id,
        camera_id=camera_id,
        ts=ts or _now(),
        track_id=track_id,
        embedding_id=embedding_id,
        role="staff",
        payload={},
    )


def pos_receipt(
    *,
    store_id: str,
    invoice_number: str,
    total_amount: float,
    item_count: int,
    salesperson_id: str | None = None,
    payment_mode: str | None = None,
    ts: datetime | None = None,
) -> Envelope:
    return _env(
        "pos_receipt",
        store_id=store_id,
        camera_id="pos",
        ts=ts,
        session_id=None,
        track_id=None,
        embedding_id=None,
        role="customer",
        payload=_PosReceipt(
            invoice_number=invoice_number,
            salesperson_id=salesperson_id,
            total_amount=total_amount,
            item_count=item_count,
            payment_mode=payment_mode,
        ),
    )


def health_warning(*, store_id: str, source: str, reason: str, ts: datetime | None = None) -> Envelope:
    return _env(
        "health_warning",
        store_id=store_id,
        camera_id=source,
        ts=ts,
        session_id=None,
        track_id=None,
        embedding_id=None,
        role="unknown",
        payload=_HealthWarning(source=source, reason=reason),
    )
