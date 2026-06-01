"""Per-camera track-state handlers.

Each handler is a pure transducer: given an (track_id, foot_point, ts)
update it returns a list of Envelopes to publish. The video worker owns
detection/tracking/I-O; this module owns "what does this camera mean".

This split lets the funnel semantics — crossings, zone dwell — be
unit-tested without OpenCV or YOLO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from services.events import (
    Envelope,
    checkout_observed,
    person_entered,
    person_exited,
    staff_observed,
    zone_dwell,
    zone_entered,
)
from services.ingest.config import CameraCfg, Tripwire, Zone
from services.ingest.geom import Point, crossed, point_in_polygon

# A track that has not been seen for this long is retired. Prevents the
# state-map from leaking when people walk off-frame.
TRACK_TTL_S = 5.0


@dataclass
class _TrackInfo:
    last_point: Point
    last_seen: datetime
    crossed_dir: str | None = None        # entry cams: which way they crossed
    zones_active: dict[str, datetime] = field(default_factory=dict)  # zone_id → entered_at


class _Base:
    role_label: str = "unknown"

    def __init__(self, cam: CameraCfg, store_id: str) -> None:
        self.cam = cam
        self.store_id = store_id
        self.tracks: dict[int, _TrackInfo] = {}

    def _stable_embedding_id(self, track_id: int) -> str:
        # Real cross-cam re-ID would substitute an OSNet embedding hash here.
        # Until that worker lands, a deterministic per-camera ID is enough
        # to bind events for a single camera into one session.
        return f"emb_{self.cam.id}_{track_id}"

    def _track_key(self, track_id: int) -> str:
        return f"{self.cam.id}_t{track_id}"

    def update(self, track_id: int, foot: Point, ts: datetime) -> list[Envelope]:  # pragma: no cover
        raise NotImplementedError

    def gc(self, now: datetime) -> list[Envelope]:
        """Retire tracks not seen for TRACK_TTL_S. Camera-specific subclasses
        override to flush remaining zone dwells before retiring."""
        expired = [tid for tid, t in self.tracks.items() if (now - t.last_seen).total_seconds() > TRACK_TTL_S]
        for tid in expired:
            self.tracks.pop(tid, None)
        return []


# --------------------------------------------------------------------------
# CAM 3 — entry/exit tripwire
# --------------------------------------------------------------------------


class EntryHandler(_Base):
    role_label = "customer"

    def __init__(self, cam: CameraCfg, store_id: str) -> None:
        super().__init__(cam, store_id)
        assert cam.tripwire is not None, "entry_exit camera must define a tripwire"
        self.tripwire: Tripwire = cam.tripwire

    def update(self, track_id: int, foot: Point, ts: datetime) -> list[Envelope]:
        out: list[Envelope] = []
        info = self.tracks.get(track_id)
        if info is None:
            self.tracks[track_id] = _TrackInfo(last_point=foot, last_seen=ts)
            return out

        direction = crossed(self.tripwire.segment, info.last_point, foot)
        # `inside_side` says which half of the frame is the store interior.
        # If inside is "left", a left_to_right crossing is an exit, etc.
        if direction != "none" and info.crossed_dir is None:
            inside = self.tripwire.inside_side
            is_entry = (
                (inside == "left"  and direction == "right_to_left") or
                (inside == "right" and direction == "left_to_right") or
                (inside == "top"   and direction == "right_to_left") or
                (inside == "bottom" and direction == "left_to_right")
            )
            common = {
                "store_id":     self.store_id,
                "camera_id":    self.cam.id,
                "line_id":      self.tripwire.line_id,
                "track_id":     self._track_key(track_id),
                "embedding_id": self._stable_embedding_id(track_id),
                "ts":           ts,
                "role":         self.role_label,
            }
            if is_entry:
                out.append(person_entered(**common))
                info.crossed_dir = "in"
            else:
                out.append(person_exited(**common))
                info.crossed_dir = "out"

        info.last_point = foot
        info.last_seen  = ts
        return out


# --------------------------------------------------------------------------
# CAM 1, CAM 2 — F.O.H shelf zones
# --------------------------------------------------------------------------


class ShelfHandler(_Base):
    role_label = "customer"

    def __init__(self, cam: CameraCfg, store_id: str) -> None:
        super().__init__(cam, store_id)
        self.zones: list[Zone] = list(cam.zones)

    def _zone_at(self, foot: Point) -> Zone | None:
        for z in self.zones:
            if point_in_polygon(z.polygon, foot):
                return z
        return None

    def update(self, track_id: int, foot: Point, ts: datetime) -> list[Envelope]:
        out: list[Envelope] = []
        info = self.tracks.setdefault(track_id, _TrackInfo(last_point=foot, last_seen=ts))

        z = self._zone_at(foot)
        common = {
            "store_id":     self.store_id,
            "track_id":     self._track_key(track_id),
            "embedding_id": self._stable_embedding_id(track_id),
            "camera_id":    self.cam.id,
            "role":         self.role_label,
            "ts":           ts,
        }

        # Close any zones the track has left.
        for zone_id, entered_at in list(info.zones_active.items()):
            if z is None or z.id != zone_id:
                dwell = (ts - entered_at).total_seconds()
                if dwell > 0:
                    out.append(zone_dwell(zone_id=zone_id, dwell_s=round(dwell, 1), **common))
                info.zones_active.pop(zone_id)

        # Open the new zone if any.
        if z is not None and z.id not in info.zones_active:
            out.append(zone_entered(zone_id=z.id,
                                    first_visit_in_session=(not info.zones_active),
                                    **common))
            info.zones_active[z.id] = ts

        info.last_point = foot
        info.last_seen = ts
        return out

    def gc(self, now: datetime) -> list[Envelope]:
        out: list[Envelope] = []
        expired = [tid for tid, t in self.tracks.items() if (now - t.last_seen).total_seconds() > TRACK_TTL_S]
        for tid in expired:
            info = self.tracks.pop(tid)
            for zone_id, entered_at in info.zones_active.items():
                dwell = (info.last_seen - entered_at).total_seconds()
                if dwell > 0:
                    out.append(zone_dwell(
                        store_id=self.store_id,
                        camera_id=self.cam.id,
                        zone_id=zone_id,
                        dwell_s=round(dwell, 1),
                        track_id=self._track_key(tid),
                        embedding_id=self._stable_embedding_id(tid),
                        ts=info.last_seen,
                        role=self.role_label,
                    ))
        return out


# --------------------------------------------------------------------------
# CAM 5 — cash counter
# --------------------------------------------------------------------------


CHECKOUT_DWELL_THRESHOLD_S = 5.0


class CashCounterHandler(_Base):
    role_label = "customer"

    def __init__(self, cam: CameraCfg, store_id: str) -> None:
        super().__init__(cam, store_id)
        self.counter_zone: Zone | None = next(
            (z for z in cam.zones if z.id == "cash_counter"), None
        )

    def update(self, track_id: int, foot: Point, ts: datetime) -> list[Envelope]:
        out: list[Envelope] = []
        info = self.tracks.setdefault(track_id, _TrackInfo(last_point=foot, last_seen=ts))
        if self.counter_zone and point_in_polygon(self.counter_zone.polygon, foot):
            if "cash_counter" not in info.zones_active:
                info.zones_active["cash_counter"] = ts
            elif "_announced" not in info.zones_active and \
                 (ts - info.zones_active["cash_counter"]).total_seconds() >= CHECKOUT_DWELL_THRESHOLD_S:
                info.zones_active["_announced"] = ts
                out.append(checkout_observed(
                    store_id=self.store_id,
                    camera_id=self.cam.id,
                    zone_id="cash_counter",
                    track_id=self._track_key(track_id),
                    embedding_id=self._stable_embedding_id(track_id),
                    queue_position=0,
                    ts=ts,
                    role=self.role_label,
                ))
        else:
            info.zones_active.clear()
        info.last_point = foot
        info.last_seen = ts
        return out


# --------------------------------------------------------------------------
# CAM 4 — back of house (staff gallery)
# --------------------------------------------------------------------------


STAFF_OBSERVE_INTERVAL_S = 30.0


class BohHandler(_Base):
    role_label = "staff"

    def update(self, track_id: int, foot: Point, ts: datetime) -> list[Envelope]:
        info = self.tracks.get(track_id)
        # Rate-limit: one staff_observed per track every 30 s.
        if info is None or (ts - info.last_seen) >= timedelta(seconds=STAFF_OBSERVE_INTERVAL_S):
            self.tracks[track_id] = _TrackInfo(last_point=foot, last_seen=ts)
            return [staff_observed(
                store_id=self.store_id,
                camera_id=self.cam.id,
                track_id=self._track_key(track_id),
                embedding_id=self._stable_embedding_id(track_id),
                ts=ts,
            )]
        info.last_point = foot
        info.last_seen = ts
        return []


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def make_handler(cam: CameraCfg, store_id: str) -> _Base:
    if cam.role == "entry_exit":
        return EntryHandler(cam, store_id)
    if cam.role in ("foh_top_shelves", "foh_bottom_shelves"):
        return ShelfHandler(cam, store_id)
    if cam.role == "cash_counter":
        return CashCounterHandler(cam, store_id)
    if cam.role == "back_of_house":
        return BohHandler(cam, store_id)
    raise ValueError(f"unknown camera role: {cam.role}")
