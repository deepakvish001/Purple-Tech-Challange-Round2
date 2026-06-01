"""Pure session state machine — no I/O.

The aggregator's brain. Given a stream of envelopes (Pydantic-validated
upstream by the bus), this module produces `SessionDelta` mutations that
the persistence layer applies to Postgres.

Decoupling state from persistence keeps the logic testable and lets the
persistence layer batch writes (one round-trip per envelope is fine for
this workload, but we don't want to bake that assumption into the
algorithm).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from services.events.schemas import Envelope

FunnelStage = Literal["entered", "browsed", "engaged", "checkout_queued", "purchased"]

# Aggregator-level tunables. These are runtime configuration in production
# (config/business.yaml) but the defaults here match docs/CHOICES.md §7.
ENGAGEMENT_DWELL_S = 20.0
POS_JOIN_WINDOW_S = 90.0
REENTRY_GAP_S = 60.0


@dataclass
class ZoneVisit:
    first_seen: datetime
    last_seen: datetime
    total_dwell_s: float = 0.0


@dataclass
class Session:
    session_id: str
    store_id: str
    embedding_id: str | None
    role: str
    entered_at: datetime
    exited_at: datetime | None = None
    funnel_stage: FunnelStage = "entered"
    checkout_at: datetime | None = None
    receipt: dict | None = None
    zones: dict[str, ZoneVisit] = field(default_factory=dict)

    def recompute_stage(self) -> None:
        # Highest stage reached wins.
        if self.receipt is not None:
            self.funnel_stage = "purchased"
            return
        if self.checkout_at is not None:
            self.funnel_stage = "checkout_queued"
            return
        if any(v.total_dwell_s >= ENGAGEMENT_DWELL_S for v in self.zones.values()):
            self.funnel_stage = "engaged"
            return
        if self.zones:
            self.funnel_stage = "browsed"
            return
        self.funnel_stage = "entered"


@dataclass
class SessionDelta:
    """A single mutation produced by `SessionStore.apply`."""

    action: Literal["opened", "updated", "closed", "noop"]
    session: Session
    matched_receipt: bool = False


class SessionStore:
    """In-memory session state. Persistence is the caller's job.

    Sessions are keyed by `embedding_id` (the appearance embedding bridges
    cameras). For envelopes without an embedding — synthetic test events,
    health warnings — `track_id` is used as a fallback key so the state
    machine still produces sensible output.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Session] = {}
        self._closed_by_key: dict[str, tuple[datetime, Session]] = {}
        self._pending_receipts: deque[Envelope] = deque(maxlen=200)

    @property
    def open_count(self) -> int:
        return len(self._by_key)

    def open_sessions(self) -> list[Session]:
        return list(self._by_key.values())

    def _key(self, env: Envelope) -> str | None:
        return env.embedding_id or env.track_id

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def apply(self, env: Envelope) -> SessionDelta:
        handler = getattr(self, f"_h_{env.type}", self._h_noop)
        return handler(env)

    # ------------------------------------------------------------------
    # Per-event handlers
    # ------------------------------------------------------------------

    def _h_person_entered(self, env: Envelope) -> SessionDelta:
        key = self._key(env)
        if key is None:
            return SessionDelta("noop", _stub_session(env))

        # Re-entry: same embedding within REENTRY_GAP_S → reopen old session.
        if key in self._closed_by_key:
            exited_at, prev = self._closed_by_key[key]
            if (env.ts - exited_at) < timedelta(seconds=REENTRY_GAP_S):
                del self._closed_by_key[key]
                prev.exited_at = None
                self._by_key[key] = prev
                return SessionDelta("updated", prev)

        sess = Session(
            session_id=str(uuid4()),
            store_id=env.store_id,
            embedding_id=env.embedding_id,
            role=env.role,
            entered_at=env.ts,
        )
        self._by_key[key] = sess
        return SessionDelta("opened", sess)

    def _h_person_exited(self, env: Envelope) -> SessionDelta:
        key = self._key(env)
        if key is None or key not in self._by_key:
            return SessionDelta("noop", _stub_session(env))
        sess = self._by_key.pop(key)
        sess.exited_at = env.ts
        sess.recompute_stage()
        self._closed_by_key[key] = (env.ts, sess)
        # Keep the closed-session cache bounded.
        if len(self._closed_by_key) > 500:
            oldest = min(self._closed_by_key, key=lambda k: self._closed_by_key[k][0])
            self._closed_by_key.pop(oldest, None)
        return SessionDelta("closed", sess)

    def _h_zone_entered(self, env: Envelope) -> SessionDelta:
        sess = self._lookup(env)
        if sess is None:
            return SessionDelta("noop", _stub_session(env))
        zone_id = env.payload["zone_id"]
        if zone_id not in sess.zones:
            sess.zones[zone_id] = ZoneVisit(first_seen=env.ts, last_seen=env.ts)
        else:
            sess.zones[zone_id].last_seen = env.ts
        sess.recompute_stage()
        return SessionDelta("updated", sess)

    def _h_zone_dwell(self, env: Envelope) -> SessionDelta:
        sess = self._lookup(env)
        if sess is None:
            return SessionDelta("noop", _stub_session(env))
        zone_id = env.payload["zone_id"]
        dwell_s = float(env.payload.get("dwell_s", 0.0))
        zv = sess.zones.get(zone_id) or ZoneVisit(first_seen=env.ts, last_seen=env.ts)
        zv.last_seen = env.ts
        zv.total_dwell_s += dwell_s
        sess.zones[zone_id] = zv
        sess.recompute_stage()
        return SessionDelta("updated", sess)

    def _h_checkout_observed(self, env: Envelope) -> SessionDelta:
        sess = self._lookup(env)
        if sess is None:
            return SessionDelta("noop", _stub_session(env))
        if sess.checkout_at is None:
            sess.checkout_at = env.ts
        # Try to match an already-arrived POS receipt.
        matched = self._try_match_pending_receipt(sess)
        sess.recompute_stage()
        return SessionDelta("updated", sess, matched_receipt=matched)

    def _h_staff_observed(self, env: Envelope) -> SessionDelta:
        # The OSNet staff gallery match would tag any session whose
        # embedding aligns. For now (synth events), tag the session whose
        # embedding_id matches directly if one is open.
        sess = self._lookup(env)
        if sess is None:
            return SessionDelta("noop", _stub_session(env))
        sess.role = "staff"
        return SessionDelta("updated", sess)

    def _h_pos_receipt(self, env: Envelope) -> SessionDelta:
        # Find the open session whose checkout_observed_at is closest to
        # the receipt ts, within POS_JOIN_WINDOW_S.
        candidates = [
            s for s in self._by_key.values()
            if s.checkout_at is not None and s.receipt is None
        ] + [
            s for _, s in self._closed_by_key.values()
            if s.checkout_at is not None and s.receipt is None
        ]
        best: Session | None = None
        best_delta = timedelta(seconds=POS_JOIN_WINDOW_S)
        for s in candidates:
            assert s.checkout_at is not None
            d = abs(env.ts - s.checkout_at)
            if d <= best_delta:
                best = s
                best_delta = d
        if best is not None:
            best.receipt = dict(env.payload)
            best.recompute_stage()
            return SessionDelta("updated", best, matched_receipt=True)
        # No match yet — buffer for a late checkout_observed.
        self._pending_receipts.append(env)
        return SessionDelta("noop", _stub_session(env))

    def _h_health_warning(self, env: Envelope) -> SessionDelta:
        return SessionDelta("noop", _stub_session(env))

    def _h_noop(self, env: Envelope) -> SessionDelta:
        return SessionDelta("noop", _stub_session(env))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lookup(self, env: Envelope) -> Session | None:
        key = self._key(env)
        if key is None:
            return None
        return self._by_key.get(key)

    def _try_match_pending_receipt(self, sess: Session) -> bool:
        assert sess.checkout_at is not None
        if not self._pending_receipts:
            return False
        best_idx: int | None = None
        best_delta = timedelta(seconds=POS_JOIN_WINDOW_S)
        for i, r in enumerate(self._pending_receipts):
            d = abs(r.ts - sess.checkout_at)
            if d <= best_delta:
                best_idx = i
                best_delta = d
        if best_idx is None:
            return False
        receipt = self._pending_receipts[best_idx]
        sess.receipt = dict(receipt.payload)
        del self._pending_receipts[best_idx]
        return True


def _stub_session(env: Envelope) -> Session:
    """Used for noop deltas — gives callers a non-None session to log."""
    return Session(
        session_id="00000000-0000-0000-0000-000000000000",
        store_id=env.store_id,
        embedding_id=env.embedding_id,
        role=env.role,
        entered_at=env.ts,
    )
