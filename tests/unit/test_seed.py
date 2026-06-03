"""Seed generator contracts — pure data, no DB needed."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from services.aggregator.seed import (
    HOUR_WEIGHTS,
    generate_demo_sessions,
    make_anomalies,
)


def test_seed_is_deterministic() -> None:
    now = datetime(2026, 6, 3, 20, 30, tzinfo=UTC)
    a = generate_demo_sessions(n=100, seed=42, now=now)
    b = generate_demo_sessions(n=100, seed=42, now=now)
    assert [s.session_id for s in a] != [s.session_id for s in b]  # UUIDs are random
    assert [s.funnel_stage for s in a] == [s.funnel_stage for s in b]
    assert [s.entered_at   for s in a] == [s.entered_at   for s in b]


def test_funnel_distribution_is_realistic() -> None:
    sessions = generate_demo_sessions(n=2000, seed=1, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    stages: Counter[str] = Counter(s.funnel_stage for s in sessions)
    total = sum(stages.values())
    # Tuned funnel: ~18% purchase, ~80% browse — leave slack for rng.
    assert 0.12 <= stages["purchased"]      / total <= 0.25
    assert 0.15 <= stages["entered"]        / total <= 0.30   # bounce rate
    assert stages["browsed"] + stages["engaged"] + stages["checkout_queued"] + stages["purchased"] > total * 0.7


def test_only_purchasers_have_receipts() -> None:
    sessions = generate_demo_sessions(n=300, seed=3, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    for s in sessions:
        if s.funnel_stage == "purchased":
            assert s.receipt is not None
            assert s.receipt["total_amount"] > 0
            assert s.receipt["item_count"] >= 1
        else:
            assert s.receipt is None


def test_checkout_at_set_iff_reached_counter() -> None:
    sessions = generate_demo_sessions(n=300, seed=4, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    for s in sessions:
        if s.funnel_stage in ("checkout_queued", "purchased"):
            assert s.checkout_at is not None
            assert s.entered_at <= s.checkout_at <= s.exited_at
        else:
            assert s.checkout_at is None


def test_zone_visits_only_for_post_entered_stages() -> None:
    sessions = generate_demo_sessions(n=300, seed=5, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    for s in sessions:
        if s.funnel_stage == "entered":
            assert s.zone_visits == []
        else:
            assert len(s.zone_visits) >= 1


def test_hour_distribution_matches_peak_pattern() -> None:
    sessions = generate_demo_sessions(n=2000, seed=7, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    by_hour: Counter[int] = Counter(s.entered_at.hour for s in sessions)
    # The peak hour (13 or 19/20) should have multiple times more traffic
    # than the quietest open hour (22).
    peak = max(by_hour[h] for h in (13, 19, 20))
    quiet = by_hour.get(22, 1)
    assert peak >= 2 * quiet


def test_anomalies_are_demoable() -> None:
    anoms = make_anomalies(now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    kinds = {a["kind"] for a in anoms}
    assert "footfall_outlier" in kinds
    assert "dead_zone" in kinds
    for a in anoms:
        assert "severity" in a
        assert "details" in a


def test_only_open_hours_seeded() -> None:
    sessions = generate_demo_sessions(n=500, seed=9, now=datetime(2026, 6, 3, 22, tzinfo=UTC))
    hours = {s.entered_at.hour for s in sessions}
    # No traffic outside operating hours (HOUR_WEIGHTS keys).
    assert hours.issubset(set(HOUR_WEIGHTS))
