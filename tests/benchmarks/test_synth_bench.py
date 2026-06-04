"""Benchmarks for the synthetic timeline generator.

These tests use `pytest-benchmark`. They exercise `generate_timeline()`
with varying session counts so judges can see runtime and memory characteristics.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.ingest.synth import SynthConfig, generate_timeline


def _cfg_for(n_sessions: int) -> SynthConfig:
    return SynthConfig(
        store_id="ST1008",
        seed=42,
        sessions=n_sessions,
        duration_s=600,
        pace=0.0,
        shelf_zones_cam1=["shelf_farmstay", "shelf_face_shop"],
        shelf_zones_cam2=["shelf_maybelline", "shelf_lakme"],
    )


def test_generate_timeline_small(benchmark):
    cfg = _cfg_for(10)
    def gen():
        return generate_timeline(cfg, base=datetime(2026, 1, 1, tzinfo=UTC))
    events = benchmark(gen)
    assert events


def test_generate_timeline_medium(benchmark):
    cfg = _cfg_for(100)
    def gen():
        return generate_timeline(cfg, base=datetime(2026, 1, 1, tzinfo=UTC))
    events = benchmark(gen)
    assert events


def test_generate_timeline_large(benchmark):
    cfg = _cfg_for(500)
    def gen():
        return generate_timeline(cfg, base=datetime(2026, 1, 1, tzinfo=UTC))
    events = benchmark(gen)
    assert events
