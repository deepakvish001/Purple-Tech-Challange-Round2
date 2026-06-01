"""SessionStore state-machine contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.aggregator.session import (
    POS_JOIN_WINDOW_S,
    REENTRY_GAP_S,
    SessionStore,
)
from services.events import (
    checkout_observed,
    person_entered,
    person_exited,
    pos_receipt,
    staff_observed,
    zone_dwell,
    zone_entered,
)

STORE = "ST1008"


def _t(s: int) -> datetime:
    return datetime(2026, 4, 10, 20, 0, 0, tzinfo=UTC) + timedelta(seconds=s)


def test_enter_then_exit_opens_and_closes_session() -> None:
    s = SessionStore()
    d1 = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                line_id="d", track_id="c3_t1",
                                embedding_id="emb_1", ts=_t(0), role="customer"))
    assert d1.action == "opened"
    assert s.open_count == 1
    sid = d1.session.session_id

    d2 = s.apply(person_exited(store_id=STORE, camera_id="cam_3_entry",
                               line_id="d", track_id="c3_t1",
                               embedding_id="emb_1", ts=_t(120), role="customer"))
    assert d2.action == "closed"
    assert d2.session.session_id == sid
    assert d2.session.exited_at == _t(120)
    assert s.open_count == 0


def test_funnel_stages_progress() -> None:
    s = SessionStore()
    sess = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                  line_id="d", track_id="c3_t1",
                                  embedding_id="emb_1", ts=_t(0))).session
    assert sess.funnel_stage == "entered"

    s.apply(zone_entered(store_id=STORE, camera_id="cam_1_top",
                         zone_id="shelf_lakme", track_id="c1_t1",
                         embedding_id="emb_1", ts=_t(20)))
    assert sess.funnel_stage == "browsed"

    s.apply(zone_dwell(store_id=STORE, camera_id="cam_1_top",
                       zone_id="shelf_lakme", dwell_s=25.0, track_id="c1_t1",
                       embedding_id="emb_1", ts=_t(45)))
    assert sess.funnel_stage == "engaged"

    s.apply(checkout_observed(store_id=STORE, camera_id="cam_5_cash",
                              zone_id="cash_counter", track_id="c5_t1",
                              embedding_id="emb_1", ts=_t(60)))
    assert sess.funnel_stage == "checkout_queued"


def test_pos_receipt_matches_recent_checkout() -> None:
    s = SessionStore()
    s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                           line_id="d", track_id="c3_t1",
                           embedding_id="emb_1", ts=_t(0)))
    sess = s.apply(checkout_observed(store_id=STORE, camera_id="cam_5_cash",
                                     zone_id="cash_counter", track_id="c5_t1",
                                     embedding_id="emb_1", ts=_t(60))).session
    d = s.apply(pos_receipt(store_id=STORE, invoice_number="INV1",
                            total_amount=999.0, item_count=2, ts=_t(90)))
    assert d.matched_receipt is True
    assert sess.receipt is not None
    assert sess.receipt["invoice_number"] == "INV1"
    assert sess.funnel_stage == "purchased"


def test_pos_receipt_buffered_until_checkout_arrives() -> None:
    s = SessionStore()
    s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                           line_id="d", track_id="c3_t1",
                           embedding_id="emb_1", ts=_t(0)))
    # POS receipt fires first.
    d_r = s.apply(pos_receipt(store_id=STORE, invoice_number="INV1",
                              total_amount=999.0, item_count=2, ts=_t(60)))
    assert d_r.matched_receipt is False
    # Then checkout_observed catches up.
    d_c = s.apply(checkout_observed(store_id=STORE, camera_id="cam_5_cash",
                                    zone_id="cash_counter", track_id="c5_t1",
                                    embedding_id="emb_1", ts=_t(65)))
    assert d_c.matched_receipt is True
    assert d_c.session.receipt["invoice_number"] == "INV1"
    assert d_c.session.funnel_stage == "purchased"


def test_pos_receipt_outside_window_does_not_match() -> None:
    s = SessionStore()
    s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                           line_id="d", track_id="c3_t1",
                           embedding_id="emb_1", ts=_t(0)))
    s.apply(checkout_observed(store_id=STORE, camera_id="cam_5_cash",
                              zone_id="cash_counter", track_id="c5_t1",
                              embedding_id="emb_1", ts=_t(60)))
    far = int(POS_JOIN_WINDOW_S) + 5
    d = s.apply(pos_receipt(store_id=STORE, invoice_number="INV1",
                            total_amount=999.0, item_count=2, ts=_t(60 + far)))
    assert d.matched_receipt is False


def test_reentry_within_gap_reopens_session() -> None:
    s = SessionStore()
    d1 = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                line_id="d", track_id="c3_t1",
                                embedding_id="emb_1", ts=_t(0)))
    s.apply(person_exited(store_id=STORE, camera_id="cam_3_entry",
                          line_id="d", track_id="c3_t1",
                          embedding_id="emb_1", ts=_t(30)))
    d2 = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                line_id="d", track_id="c3_t1",
                                embedding_id="emb_1", ts=_t(60)))
    assert d2.action == "updated"
    assert d2.session.session_id == d1.session.session_id


def test_reentry_beyond_gap_opens_new_session() -> None:
    s = SessionStore()
    d1 = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                line_id="d", track_id="c3_t1",
                                embedding_id="emb_1", ts=_t(0)))
    s.apply(person_exited(store_id=STORE, camera_id="cam_3_entry",
                          line_id="d", track_id="c3_t1",
                          embedding_id="emb_1", ts=_t(30)))
    gap = int(REENTRY_GAP_S) + 30
    d2 = s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                                line_id="d", track_id="c3_t1",
                                embedding_id="emb_1", ts=_t(30 + gap)))
    assert d2.action == "opened"
    assert d2.session.session_id != d1.session.session_id


def test_staff_observed_tags_session_as_staff() -> None:
    s = SessionStore()
    s.apply(person_entered(store_id=STORE, camera_id="cam_3_entry",
                           line_id="d", track_id="c3_t1",
                           embedding_id="emb_staff", ts=_t(0)))
    d = s.apply(staff_observed(store_id=STORE, camera_id="cam_4_boh",
                               track_id="c4_t1", embedding_id="emb_staff", ts=_t(5)))
    assert d.session.role == "staff"


def test_orphan_zone_event_is_noop() -> None:
    s = SessionStore()
    d = s.apply(zone_entered(store_id=STORE, camera_id="cam_1_top",
                             zone_id="shelf_lakme", track_id="c1_orphan",
                             embedding_id="emb_unknown", ts=_t(0)))
    assert d.action == "noop"
    assert s.open_count == 0
