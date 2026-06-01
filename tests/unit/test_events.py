"""Schema and helper contracts for services.events."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.events import (
    Envelope,
    checkout_observed,
    health_warning,
    person_entered,
    person_exited,
    pos_receipt,
    staff_observed,
    zone_dwell,
    zone_entered,
)
from services.events.schemas import EVENT_TYPES


def test_envelope_requires_tz_aware_ts() -> None:
    with pytest.raises(ValueError):
        Envelope(
            type="person_entered",
            store_id="ST1008",
            ts=datetime(2026, 4, 10, 20, 0, 0),  # naive
            payload={},
        )


def test_envelope_normalises_to_utc() -> None:
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    ist = _tz(_td(hours=5, minutes=30))
    env = Envelope(
        type="person_entered",
        store_id="ST1008",
        ts=datetime(2026, 4, 10, 20, 0, 0, tzinfo=ist),
        payload={},
    )
    assert env.ts.tzinfo == UTC


def test_round_trip_json() -> None:
    env = person_entered(
        store_id="ST1008",
        camera_id="cam_3_entry",
        line_id="door_main",
        track_id="c3_track_5",
    )
    blob = env.to_json()
    back = Envelope.from_json(blob)
    assert back.type == "person_entered"
    assert back.payload["direction"] == "in"
    assert back.payload["line_id"] == "door_main"
    assert back.track_id == "c3_track_5"
    assert back.ts == env.ts


def test_event_types_are_complete() -> None:
    # If we add a new event type, this test forces us to teach the
    # constructor helpers as well.
    must_have = {
        "person_entered",
        "person_exited",
        "zone_entered",
        "zone_dwell",
        "checkout_observed",
        "staff_observed",
        "pos_receipt",
        "health_warning",
    }
    assert must_have.issubset(set(EVENT_TYPES))


def test_all_constructors() -> None:
    common = {"store_id": "ST1008"}
    envs = [
        person_entered(camera_id="cam_3_entry", line_id="d", track_id="t1", **common),
        person_exited(camera_id="cam_3_entry", line_id="d", track_id="t1", session_duration_s=123.4, **common),
        zone_entered(camera_id="cam_1_top", zone_id="shelf_lakme", track_id="t1", **common),
        zone_dwell(camera_id="cam_1_top", zone_id="shelf_lakme", dwell_s=42.0, track_id="t1", **common),
        checkout_observed(camera_id="cam_5_cash", zone_id="cash_counter", track_id="t1", queue_position=1, **common),
        staff_observed(camera_id="cam_4_boh", track_id="staff_1", **common),
        pos_receipt(invoice_number="ML0426KAP0001358", total_amount=274.36, item_count=1, salesperson_id="1178", **common),
        health_warning(source="cam_2_bottom", reason="frame_drop", **common),
    ]
    for e in envs:
        # All envelopes must round-trip cleanly.
        assert Envelope.from_json(e.to_json()) == e
    assert {e.type for e in envs} == {
        "person_entered",
        "person_exited",
        "zone_entered",
        "zone_dwell",
        "checkout_observed",
        "staff_observed",
        "pos_receipt",
        "health_warning",
    }


def test_extra_fields_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Envelope(
            type="person_entered",
            store_id="ST1008",
            ts=datetime.now(UTC),
            payload={},
            extra_field="bad",  # type: ignore[call-arg]
        )
