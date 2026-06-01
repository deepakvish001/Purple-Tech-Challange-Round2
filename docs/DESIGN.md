# DESIGN.md — Store Intelligence System

## 1. Problem framing

The store at Brigade Road has one customer-facing entry/exit (left wall, glass
door) and a long rectangular floor with brand shelves lining the top and
bottom walls, a makeup unit and a nail/fragrance unit in the centre F.O.H,
and the cash counter + PMU station on the right.

Business questions we need to answer from raw CCTV + POS data:

| Question | Metric | Source |
|---|---|---|
| How many people walked in today? | `footfall` | CCTV entry line crossings |
| Which brand shelves get attention? | `zone_dwell`, `zone_unique_visitors` | CCTV zone events |
| What's our funnel? | `enter → browse → engage → checkout → purchase` | CCTV + POS join |
| What's our conversion rate? | `purchases / footfall` per hour | CCTV + POS |
| Anything unusual? | hourly footfall z-score, conv-rate drop, dead zones | aggregator |

The challenge explicitly values **engineering judgment over model
complexity**, so the design optimises for: (a) one-command bring-up,
(b) clean event schema that survives detection noise, and
(c) session-based funnel logic that does not double-count.

## 2. High-level architecture

```
┌───────────────┐    frames     ┌──────────────────┐   detection_events
│ Video source  │──────────────▶│ Ingest service   │─────────────┐
│ (file / RTSP) │               │ YOLO + ByteTrack │             │
└───────────────┘               │ + zone mapper    │             ▼
                                └──────────────────┘     ┌───────────────┐
                                                         │ Redis Streams │
┌───────────────┐    receipts   ┌──────────────────┐     │  events:*     │
│ POS CSV       │──────────────▶│ POS ingester     │────▶│               │
└───────────────┘               └──────────────────┘     └───────┬───────┘
                                                                 │ consume
                                                                 ▼
                                                         ┌───────────────┐
                                                         │ Aggregator    │
                                                         │  sessions,    │
                                                         │  funnel,      │
                                                         │  anomalies    │
                                                         └───────┬───────┘
                                                                 │ writes
                                                                 ▼
                                                         ┌───────────────┐
                                                         │ Postgres      │
                                                         │ (analytics)   │
                                                         └───────┬───────┘
                                                                 │
                            ┌────────────────────────────────────┴──────┐
                            ▼                                           ▼
                    ┌───────────────┐                          ┌───────────────┐
                    │ FastAPI       │                          │ Streamlit     │
                    │ /metrics      │                          │ dashboard     │
                    │ /funnel       │◀─────────────────────────│               │
                    │ /anomalies    │                          └───────────────┘
                    └───────────────┘
```

Each box is one container in `docker-compose.yml`. Redis Streams is the
canonical event bus; Postgres holds the materialised aggregates the API reads
from. The aggregator is the only writer to Postgres.

## 3. Detection pipeline

**Frame source.** `services/ingest` reads an MP4 file (or RTSP URL) via
OpenCV. Frame rate is decoupled from wall clock — we forward a monotonic
`frame_ts` derived from the source timestamp so the pipeline produces the
same events whether we play back at 1× or 4×.

**Detection.** YOLOv8n (`ultralytics`) with `classes=[0]` (person).
Lightweight, runs on CPU at a few FPS, GPU if available. The detection
threshold (0.4) is conservative — we'd rather miss a frame than spawn
phantom tracks.

**Tracking.** ByteTrack via `supervision`. ByteTrack is robust to short
occlusions (people passing behind the makeup chairs) because it keeps
low-confidence detections in a secondary association pass. Track IDs are
the unit of identity throughout the system.

**Entry/exit counting.** A single virtual *tripwire* is drawn across the
door corridor on the left wall. A track is counted as `person_entered` when
its centroid crosses the line in the inward direction and the track has at
least *N* frames of history (debounces shimmer). The reverse triggers
`person_exited`. The line is configured in `config/zones.yaml`, not
hard-coded.

**Zone mapping.** The store layout PNG (`docs/store_layout.png`, drawn from
the supplied Excel) defines polygons for each shelf cluster, the F.O.H, the
nail unit, the makeup unit, the cash counter, and the PMU. A one-time
homography (`H`) is computed by clicking ≥4 corresponding points between the
camera view and the floor plan; the result is stored in
`config/homography.json`. At runtime we transform each track's foot point
(bottom-centre of the bbox) into floor coordinates and lookup the zone.

**Edge cases we explicitly handle:**

| Case | Approach |
|---|---|
| **Re-entry within the visit** | Tracks that exit the entry-line and re-cross within `REENTRY_GAP_S` (default 60 s) reuse the same `session_id` via short-term re-identification on appearance embeddings. Beyond the gap, a new session is opened — matches retail intuition (stepping back out vs returning later). |
| **Staff / salespeople** | The POS CSV gives us the closing salesperson roster (`salesperson_id`, `salesperson_name`). At startup we sample N appearance crops of each ID near the cash counter and PMU during the opening hour. Tracks whose embedding distance to any staff centroid stays below a threshold are tagged `role=staff` and excluded from `footfall`. Falls back to a "lingers > 30 min and frequents back-of-house zones" heuristic when no embedding match is found. |
| **Group entry** | Each track is counted independently. We additionally publish `group_id` when two or more tracks enter within 2 s of each other and stay within 1.5 m on the floor plan for ≥ 10 s — so conversion can be measured per-group too. |
| **Occlusion behind makeup unit** | ByteTrack's low-conf second pass handles short gaps. For longer gaps (> 1 s) we keep the track in a `lost` state for up to 5 s before retiring it, which prevents inflating entry counts when someone steps behind a fixture. |
| **Camera glare / empty frames** | Sliding window: if detection variance collapses to zero for > 30 s during operating hours, the ingester emits a `health_warning` event instead of silently producing nothing. |

## 4. Event schema

All events are JSON, written to Redis Stream `events` with the
`type` field as a routing key. Common envelope:

```json
{
  "event_id": "uuid",
  "type": "person_entered",
  "store_id": "ST1008",
  "camera_id": "cam_entrance",
  "ts": "2026-04-10T16:55:36.412+05:30",
  "session_id": "sess_…",
  "track_id": 4127,
  "role": "customer",
  "payload": { … type-specific … }
}
```

| Event type | `payload` |
|---|---|
| `person_entered` | `{ direction: "in", line_id: "door_main" }` |
| `person_exited` | `{ direction: "out", line_id: "door_main", duration_s: 312 }` |
| `zone_entered` | `{ zone_id: "shelf_lakme", first_visit: true }` |
| `zone_dwell` | `{ zone_id, dwell_s: 47.2 }` — emitted on zone exit |
| `checkout_observed` | `{ zone_id: "cash_counter", queue_pos: 2 }` |
| `pos_receipt` | `{ invoice_number, salesperson_id, total_amount, items: N }` |
| `health_warning` | `{ source, reason }` |

Full schema with JSON Schema validation lives in
[`docs/EVENT_SCHEMA.md`](EVENT_SCHEMA.md).

## 5. Sessions, funnel, and conversion

A **session** is one customer visit. It opens on `person_entered`, closes on
the matching `person_exited`, and is the unit against which all funnel and
conversion stats are computed — so a single visitor cannot be double-counted
no matter how many shelves they touch.

**Funnel stages** (each session can reach at most one terminal stage):

1. `entered` — entry-line crossing.
2. `browsed` — has any `zone_entered` for a shelf zone.
3. `engaged` — has `zone_dwell ≥ 20 s` in any shelf or the makeup unit.
4. `checkout_queued` — has `checkout_observed` at the cash counter zone.
5. `purchased` — a `pos_receipt` lands within ±90 s of the session's
   `checkout_observed` time window (best-effort timestamp join — the POS
   has no track ID).

The choice of ±90 s and the dwell threshold are configurable; see
CHOICES.md §4.

**Conversion rate** = `purchased / entered`, computed per hour and per day.

## 6. APIs

FastAPI (`services/api`). All responses are JSON; query params standard.

| Endpoint | Returns |
|---|---|
| `GET /metrics?from=…&to=…` | footfall, unique sessions, avg dwell, conversion |
| `GET /funnel?from=…&to=…` | counts per funnel stage + drop-off % |
| `GET /zones` | per-zone unique visitors, total dwell, peak hour |
| `GET /anomalies?from=…&to=…` | list of detected anomalies with severity |
| `GET /sessions/{id}` | full timeline of a single session |
| `GET /healthz` `/readyz` | liveness / readiness |
| `GET /metrics-prom` | Prometheus exposition |

OpenAPI spec is auto-published at `/docs`.

## 7. Anomaly detection

Three families, all running on a 1-minute schedule inside the aggregator:

1. **Footfall outlier** — rolling 7-day same-weekday-same-hour mean & std;
   flag if `|z| > 2.5`.
2. **Conversion drop** — compare current-hour conversion against prior-3-hour
   mean; flag if drop > 30 % and footfall > 20.
3. **Dead zone** — a shelf zone whose unique-visitor count over the last hour
   is < 25 % of its 14-day median, during operating hours.

Each anomaly is persisted as a row and surfaced through `/anomalies`.

## 8. Production readiness

- **Deployment**: single `docker compose up`. Health checks gate startup
  order (Redis → Postgres → aggregator → api/dashboard).
- **Observability**:
  - Structured JSON logs with `trace_id` propagated through Redis message
    headers.
  - Prometheus metrics: events-per-second, processing lag, API latency
    histograms.
  - OpenTelemetry traces from API → Postgres exported to the optional
    `otel-collector` container.
- **Testing**:
  - Unit tests for tripwire crossing, zone polygon lookup, session
    state-machine, POS-timestamp join.
  - Integration test that replays a 30 s fixture clip + synthetic POS rows
    and asserts a known event sequence and final `/metrics` payload.
  - CI workflow runs `pytest` + `ruff` + `mypy` on every push.

## 9. Out of scope (by design)

- Multi-camera fusion (the store has one usable entry view).
- Person re-identification across days.
- Demographic inference (age/gender) — both ethically fraught and out of the
  evaluation rubric.
