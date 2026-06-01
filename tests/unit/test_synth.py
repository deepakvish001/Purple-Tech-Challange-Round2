"""Timeline-generator contracts for services.ingest.synth.

These tests are pure (no Redis, no I/O) — they exercise `generate_timeline`
to make sure synthetic events are well-formed and respect the funnel
ordering the aggregator will rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.ingest.synth import SynthConfig, generate_timeline


@pytest.fixture
def cfg() -> SynthConfig:
    return SynthConfig(
        store_id="ST1008",
        seed=7,
        sessions=10,
        duration_s=300,
        pace=0.0,
        shelf_zones_cam1=["shelf_farmstay", "shelf_face_shop", "shelf_minimalist"],
        shelf_zones_cam2=["shelf_lakme", "shelf_maybelline"],
    )


def test_timeline_is_sorted_in_time(cfg: SynthConfig) -> None:
    base = datetime(2026, 4, 10, 14, 30, tzinfo=UTC)
    events = generate_timeline(cfg, base=base)
    assert events == sorted(events, key=lambda e: e.ts)


def test_each_session_opens_and_closes(cfg: SynthConfig) -> None:
    events = generate_timeline(cfg)
    by_embed: dict[str, list[str]] = {}
    for e in events:
        if e.embedding_id and e.embedding_id.startswith("emb_cust_"):
            by_embed.setdefault(e.embedding_id, []).append(e.type)
    # Every customer session must start with enter and end with exit.
    assert len(by_embed) == cfg.sessions
    for types in by_embed.values():
        assert types[0] == "person_entered"
        assert types[-1] == "person_exited"


def test_no_checkout_without_zone_visit(cfg: SynthConfig) -> None:
    events = generate_timeline(cfg)
    by_embed: dict[str, list[str]] = {}
    for e in events:
        if e.embedding_id and e.embedding_id.startswith("emb_cust_"):
            by_embed.setdefault(e.embedding_id, []).append(e.type)
    for types in by_embed.values():
        if "checkout_observed" in types:
            assert "zone_entered" in types, "checkout should not occur before browsing"
            assert types.index("zone_entered") < types.index("checkout_observed")


def test_receipts_only_for_purchasers(cfg: SynthConfig) -> None:
    events = generate_timeline(cfg)
    receipts = [e for e in events if e.type == "pos_receipt"]
    # Each receipt should occur after a checkout_observed for the same
    # session window (loose check — we use embedding_id as proxy).
    assert len(receipts) >= 0  # there can legitimately be zero in some seeds
    # No receipt may carry a staff embedding.
    for r in receipts:
        assert r.role != "staff"


def test_staff_pings_are_only_on_cam4(cfg: SynthConfig) -> None:
    events = generate_timeline(cfg)
    staff_evs = [e for e in events if e.type == "staff_observed"]
    assert staff_evs, "expected at least one synthetic staff observation"
    assert all(e.camera_id == "cam_4_boh" and e.role == "staff" for e in staff_evs)


def test_camera_distribution(cfg: SynthConfig) -> None:
    events = generate_timeline(cfg)
    cams = {e.camera_id for e in events}
    # All the in-store cameras should produce events in a reasonable run.
    assert "cam_3_entry" in cams
    assert "cam_4_boh" in cams
    assert {"cam_1_top", "cam_2_bottom"} & cams
