"""Pure 2-D geometry used by the per-camera video workers.

No NumPy / OpenCV dependency — these are deliberately small so the unit
tests pin tripwire and zone semantics without needing the heavy stack.
"""

from __future__ import annotations

from typing import Literal

Point = tuple[float, float]
Segment = tuple[Point, Point]
Polygon = list[Point]

Side = Literal["left", "right", "on"]


def signed_side(line: Segment, p: Point) -> float:
    """Return a number whose sign indicates which side of the directed
    line segment (a→b) the point p lies on.

    In screen coordinates (y grows downward), positive = visually *left*
    of the directed segment and negative = visually *right*. This matches
    how a reviewer would describe the CAM 3 tripwire ("mall corridor is
    on the right, store interior on the left").
    """
    (ax, ay), (bx, by) = line
    return (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)


def line_side(line: Segment, p: Point) -> Side:
    s = signed_side(line, p)
    if s == 0:
        return "on"
    return "left" if s > 0 else "right"


def crossed(line: Segment, prev: Point, curr: Point) -> Literal["none", "left_to_right", "right_to_left"]:
    """Direction of crossing between two consecutive foot points."""
    sp = signed_side(line, prev)
    sc = signed_side(line, curr)
    if sp == 0 or sc == 0 or (sp > 0) == (sc > 0):
        return "none"
    return "left_to_right" if sp > 0 else "right_to_left"


def point_in_polygon(poly: Polygon, p: Point) -> bool:
    """Ray-casting point-in-polygon for non-self-intersecting polygons."""
    x, y = p
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def foot_point(xyxy: tuple[float, float, float, float]) -> Point:
    """Bottom-centre of the bbox — used as the track's standing position."""
    x1, _y1, x2, y2 = xyxy
    return ((x1 + x2) / 2.0, y2)
