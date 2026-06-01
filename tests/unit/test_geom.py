"""2-D geometry primitives used by the per-camera workers."""

from __future__ import annotations

from services.ingest.geom import (
    crossed,
    foot_point,
    line_side,
    point_in_polygon,
    signed_side,
)

# A vertical tripwire on CAM 3 (x = 960, from top to bottom). Right of the
# line is the mall corridor, left is the store interior.
TRIPWIRE = ((960, 60), (960, 1020))


def test_line_side_classifies_left_right_on() -> None:
    assert line_side(TRIPWIRE, (500, 500)) == "left"
    assert line_side(TRIPWIRE, (1500, 500)) == "right"
    assert line_side(TRIPWIRE, (960, 500)) == "on"


def test_signed_side_has_consistent_sign() -> None:
    # Moving the test point further left should make |signed_side| grow.
    # In screen coords with the tripwire pointing downward, points to the
    # visual left of the line give a positive signed_side.
    s1 = signed_side(TRIPWIRE, (800, 500))
    s2 = signed_side(TRIPWIRE, (500, 500))
    assert s1 > 0 and s2 > 0
    assert s2 > s1


def test_crossing_directions() -> None:
    # Right (mall) → left (store) is an entry.
    assert crossed(TRIPWIRE, (1100, 500), (800, 500)) == "right_to_left"
    # Left (store) → right (mall) is an exit.
    assert crossed(TRIPWIRE, (800, 500), (1100, 500)) == "left_to_right"
    # Same-side movement does not cross.
    assert crossed(TRIPWIRE, (800, 500), (700, 500)) == "none"
    assert crossed(TRIPWIRE, (1100, 500), (1200, 500)) == "none"


def test_point_on_line_returns_no_crossing() -> None:
    # Touching the line itself doesn't count — we wait for a clean cross.
    assert crossed(TRIPWIRE, (960, 500), (800, 500)) == "none"
    assert crossed(TRIPWIRE, (800, 500), (960, 500)) == "none"


def test_point_in_polygon_basic_shapes() -> None:
    # Rectangle (a typical zone polygon).
    poly = [(0, 0), (240, 0), (240, 600), (0, 600)]
    assert point_in_polygon(poly, (120, 300))
    assert not point_in_polygon(poly, (300, 300))
    assert not point_in_polygon(poly, (120, 800))

    # Concave polygon (L-shape) — the "elbow" exterior point must not be
    # classified as inside.
    L = [(0, 0), (200, 0), (200, 100), (100, 100), (100, 200), (0, 200)]
    assert point_in_polygon(L, (50, 50))
    assert point_in_polygon(L, (50, 150))
    assert not point_in_polygon(L, (150, 150))  # the carved-out quadrant


def test_foot_point_is_bottom_centre_of_bbox() -> None:
    assert foot_point((10.0, 20.0, 30.0, 80.0)) == (20.0, 80.0)
    assert foot_point((0.0, 0.0, 100.0, 200.0)) == (50.0, 200.0)
