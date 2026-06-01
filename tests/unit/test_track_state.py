"""Per-camera track-state handlers.

These tests exercise the contract between the video worker and the
downstream pipeline: given a sequence of (track_id, foot_point, ts), the
right events fire. No video / no models — pure transducer behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.ingest.config import CameraCfg, Tripwire, Zone
from services.ingest.track_state import (
    BohHandler,
    CashCounterHandler,
    EntryHandler,
    ShelfHandler,
    make_handler,
)


def _t(s: float) -> datetime:
    return datetime(2026, 4, 10, 20, 0, 0, tzinfo=UTC) + timedelta(seconds=s)


# --------------------------------------------------------------------------
# Entry / exit (CAM 3)
# --------------------------------------------------------------------------


def _entry_cam() -> CameraCfg:
    return CameraCfg(
        id="cam_3_entry",
        role="entry_exit",
        source=None,
        tripwire=Tripwire(line_id="door_main", segment=((960, 60), (960, 1020)), inside_side="left"),
    )


def test_right_to_left_crossing_emits_person_entered() -> None:
    h = EntryHandler(_entry_cam(), "ST1008")
    h.update(1, (1100, 500), _t(0))    # corridor side, first sighting
    evs = h.update(1, (800, 500), _t(1))  # walked into the store
    assert len(evs) == 1
    assert evs[0].type == "person_entered"
    assert evs[0].payload["line_id"] == "door_main"


def test_left_to_right_crossing_emits_person_exited() -> None:
    h = EntryHandler(_entry_cam(), "ST1008")
    h.update(1, (800, 500), _t(0))     # store interior
    evs = h.update(1, (1100, 500), _t(1))  # walked out
    assert evs and evs[0].type == "person_exited"


def test_no_crossing_when_track_stays_on_one_side() -> None:
    h = EntryHandler(_entry_cam(), "ST1008")
    h.update(1, (800, 500), _t(0))
    assert h.update(1, (700, 500), _t(1)) == []
    assert h.update(1, (750, 500), _t(2)) == []


def test_second_crossing_for_same_track_is_suppressed() -> None:
    # We only emit one envelope per track per crossing direction so a
    # person standing on the threshold doesn't flood the bus.
    h = EntryHandler(_entry_cam(), "ST1008")
    h.update(1, (1100, 500), _t(0))
    evs1 = h.update(1, (800, 500), _t(1))
    evs2 = h.update(1, (1100, 500), _t(2))   # they oscillate back
    assert len(evs1) == 1
    assert len(evs2) == 0


# --------------------------------------------------------------------------
# Shelf zones (CAM 1 / CAM 2)
# --------------------------------------------------------------------------


def _shelf_cam() -> CameraCfg:
    return CameraCfg(
        id="cam_1_top",
        role="foh_top_shelves",
        source=None,
        zones=[
            Zone(id="shelf_lakme",  label="Lakme",  polygon=[(0, 0), (200, 0), (200, 400), (0, 400)]),
            Zone(id="shelf_loreal", label="L'Oreal", polygon=[(200, 0), (400, 0), (400, 400), (200, 400)]),
        ],
    )


def test_entering_zone_emits_zone_entered() -> None:
    h = ShelfHandler(_shelf_cam(), "ST1008")
    evs = h.update(1, (100, 200), _t(0))
    assert [e.type for e in evs] == ["zone_entered"]
    assert evs[0].payload["zone_id"] == "shelf_lakme"
    assert evs[0].payload["first_visit_in_session"] is True


def test_dwell_accumulates_then_emits_on_zone_exit() -> None:
    h = ShelfHandler(_shelf_cam(), "ST1008")
    h.update(1, (100, 200), _t(0))            # enter Lakme
    h.update(1, (100, 200), _t(15))           # still in Lakme
    evs = h.update(1, (300, 200), _t(20))     # walks to L'Oreal
    types = [e.type for e in evs]
    assert "zone_dwell" in types and "zone_entered" in types
    dwell = next(e for e in evs if e.type == "zone_dwell")
    assert dwell.payload["zone_id"] == "shelf_lakme"
    assert dwell.payload["dwell_s"] == 20.0


def test_walking_off_screen_flushes_open_zone_in_gc() -> None:
    h = ShelfHandler(_shelf_cam(), "ST1008")
    h.update(1, (100, 200), _t(0))
    h.update(1, (100, 200), _t(10))
    # No update for >TTL — gc should flush the dwell.
    out = h.gc(_t(60))
    assert any(e.type == "zone_dwell" and e.payload["zone_id"] == "shelf_lakme" for e in out)


def test_track_outside_any_zone_is_silent() -> None:
    h = ShelfHandler(_shelf_cam(), "ST1008")
    assert h.update(1, (1000, 1000), _t(0)) == []


# --------------------------------------------------------------------------
# CAM 5 — cash counter
# --------------------------------------------------------------------------


def _cash_cam() -> CameraCfg:
    return CameraCfg(
        id="cam_5_cash",
        role="cash_counter",
        source=None,
        zones=[
            Zone(id="cash_counter", label="Cash", polygon=[(0, 0), (500, 0), (500, 500), (0, 500)]),
        ],
    )


def test_short_pass_through_counter_does_not_announce() -> None:
    h = CashCounterHandler(_cash_cam(), "ST1008")
    assert h.update(1, (100, 100), _t(0)) == []
    assert h.update(1, (100, 100), _t(2)) == []  # under threshold


def test_lingering_at_counter_emits_checkout_observed_once() -> None:
    h = CashCounterHandler(_cash_cam(), "ST1008")
    h.update(1, (100, 100), _t(0))
    h.update(1, (100, 100), _t(3))
    evs = h.update(1, (100, 100), _t(7))   # >= 5 s threshold
    assert [e.type for e in evs] == ["checkout_observed"]
    # Second tick should not re-emit while still at the counter.
    evs2 = h.update(1, (100, 100), _t(12))
    assert evs2 == []


# --------------------------------------------------------------------------
# CAM 4 — staff
# --------------------------------------------------------------------------


def _boh_cam() -> CameraCfg:
    return CameraCfg(id="cam_4_boh", role="back_of_house", source=None)


def test_staff_observed_rate_limited_to_30s() -> None:
    h = BohHandler(_boh_cam(), "ST1008")
    evs1 = h.update(1, (100, 100), _t(0))
    evs2 = h.update(1, (100, 100), _t(10))   # within rate limit
    evs3 = h.update(1, (100, 100), _t(45))   # past it
    assert [e.type for e in evs1] == ["staff_observed"]
    assert evs2 == []
    assert [e.type for e in evs3] == ["staff_observed"]
    assert evs1[0].role == "staff" and evs3[0].role == "staff"


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def test_factory_picks_correct_handler() -> None:
    assert isinstance(make_handler(_entry_cam(), "ST1008"), EntryHandler)
    assert isinstance(make_handler(_shelf_cam(), "ST1008"), ShelfHandler)
    assert isinstance(make_handler(_cash_cam(), "ST1008"), CashCounterHandler)
    assert isinstance(make_handler(_boh_cam(), "ST1008"), BohHandler)
