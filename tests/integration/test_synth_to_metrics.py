"""End-to-end test: synth timeline → SessionStore → funnel metrics.

No infra required. Exercises the whole logical pipeline (timeline
generation, session resolution, funnel stage classification, POS join)
against a deterministic seed so the assertions are stable.

This is the test that catches regressions in the *contract* between the
synthetic publisher and the aggregator's state machine — i.e. when a new
event type is added, or when funnel thresholds change.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest

from services.aggregator.session import SessionStore
from services.ingest.synth import SynthConfig, generate_timeline


@pytest.fixture
def store_and_timeline() -> tuple[SessionStore, list]:
    cfg = SynthConfig(
        store_id="ST1008",
        seed=11,
        sessions=40,
        duration_s=900,
        pace=0.0,
        shelf_zones_cam1=["shelf_farmstay", "shelf_face_shop", "shelf_minimalist"],
        shelf_zones_cam2=["shelf_lakme", "shelf_maybelline", "shelf_swiss_beauty"],
    )
    base = datetime(2026, 4, 10, 14, 30, tzinfo=UTC)
    events = generate_timeline(cfg, base=base)
    store = SessionStore()
    for ev in events:
        store.apply(ev)
    return store, events


def test_session_count_matches_entries(store_and_timeline) -> None:
    store, events = store_and_timeline
    entries = sum(1 for e in events if e.type == "person_entered")
    # All customer sessions should have closed (synth emits exit for each).
    assert store.open_count == 0
    # We can't peek at closed-session count directly; assert via events.
    exits = sum(1 for e in events if e.type == "person_exited")
    assert entries == exits


def test_pos_receipts_attribute_to_sessions(store_and_timeline) -> None:
    _store, events = store_and_timeline
    # Re-run with a fresh store, watching matched_receipt deltas.
    s = SessionStore()
    matched = 0
    receipts_seen = 0
    for ev in events:
        delta = s.apply(ev)
        if ev.type == "pos_receipt":
            receipts_seen += 1
        if delta.matched_receipt:
            matched += 1
    # At least 80 % of synthetic receipts should attribute to a session
    # — the others are within the buffer window. With this seed it's 100 %.
    assert receipts_seen > 0
    assert matched >= int(0.8 * receipts_seen)


def test_funnel_proportions_are_sensible(store_and_timeline) -> None:
    _store, events = store_and_timeline
    # Replay through a fresh store so we can read final per-session stages.
    s = SessionStore()
    for ev in events:
        s.apply(ev)
    # Walk closed sessions out via internal accessor (test-only).
    closed = [sess for _, sess in s._closed_by_key.values()]
    by_stage: Counter[str] = Counter(sess.funnel_stage for sess in closed)

    # Every session must have made it through "entered".
    total = sum(by_stage.values())
    assert total > 0

    # Funnel monotonicity (downstream stage ≤ upstream stage when expressed
    # cumulatively).
    cum_browsed   = by_stage["browsed"]   + by_stage["engaged"]  + by_stage["checkout_queued"] + by_stage["purchased"]
    cum_engaged   = by_stage["engaged"]   + by_stage["checkout_queued"] + by_stage["purchased"]
    cum_checkout  = by_stage["checkout_queued"] + by_stage["purchased"]
    cum_purchased = by_stage["purchased"]

    assert cum_browsed   >= cum_engaged
    assert cum_engaged   >= cum_checkout
    assert cum_checkout  >= cum_purchased

    # Sanity: at least some sessions should engage and some should purchase.
    assert cum_engaged   > 0, "no sessions reached engaged stage — funnel broken"
    assert cum_purchased > 0, "no sessions purchased — POS join broken"


def test_no_orphan_pos_receipts_in_pending_after_replay(store_and_timeline) -> None:
    _store, events = store_and_timeline
    s = SessionStore()
    for ev in events:
        s.apply(ev)
    # Synth always emits pos_receipt after a matching checkout_observed
    # within the join window, so the buffer should drain fully.
    assert len(s._pending_receipts) == 0


def test_camera_distribution_covers_all_in_store_cams(store_and_timeline) -> None:
    _store, events = store_and_timeline
    cams = Counter(e.camera_id for e in events if e.camera_id and e.camera_id.startswith("cam_"))
    # Every in-store camera should produce events in a 40-session timeline.
    for expected in ("cam_1_top", "cam_2_bottom", "cam_3_entry", "cam_4_boh", "cam_5_cash"):
        assert cams[expected] > 0, f"camera {expected} produced no events"
