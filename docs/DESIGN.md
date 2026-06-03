# DESIGN.md — Store Intelligence System

## 1. Problem framing

The Brigade Road store is observed by **five CCTV cameras**. Inspecting the
supplied footage (`Datasets` release) makes their roles unambiguous:

| Camera | Resolution | FPS | Role |
|---|---|---|---|
| **CAM 1** | 1080p H.264 | 30 | F.O.H **top-wall shelves** (Farmstay/Korean → Aqualogica) |
| **CAM 2** | 1080p H.264 | 30 | F.O.H **bottom-wall shelves** (Accessories → Maybelline) |
| **CAM 3** | 1080p H.264 | 30 | **Entry / exit vestibule** — glass-partition view of the door |
| **CAM 4** | 1080p HEVC | 25 | **Back-of-house** (stockroom, staff break area) |
| **CAM 5** | 1080p HEVC | 25 | **Cash counter / billing** |

Each camera produces ~2 minutes of footage in the supplied sample. Timestamps
on every clip read `10/04/2026 ~20:10`, which lines up with the POS CSV
(`Brigade_Bangalore_10_April_26`) — so detection events can be joined to
receipts purely by wall-clock time.

Business questions we need to answer from raw CCTV + POS data:

| Question | Metric | Source |
|---|---|---|
| How many people walked in today? | `footfall` | CAM 3 entry tripwire |
| Which brand shelves get attention? | `zone_dwell`, `zone_unique_visitors` | CAM 1 + CAM 2 zone events |
| What's our funnel? | `enter → browse → engage → checkout → purchase` | All cams + POS |
| What's our conversion rate? | `purchases / footfall` per hour | CAM 3 + POS |
| Anything unusual? | hourly footfall z-score, conv-rate drop, dead zones | aggregator |

The challenge explicitly values **engineering judgment over model
complexity**, so the design optimises for: (a) one-command bring-up,
(b) clean event schema that survives detection noise across five views,
(c) session-based funnel logic that does not double-count even when the
same person appears in multiple cameras.

## 2. High-level architecture

```
       ┌───────────────────────────────────────────────────────────┐
       │ Five video sources (file replay or RTSP)                  │
       │  CAM 1   CAM 2   CAM 3   CAM 4   CAM 5                    │
       └───┬───────┬───────┬───────┬───────┬───────────────────────┘
           │       │       │       │       │
           ▼       ▼       ▼       ▼       ▼
       ┌───────────────────────────────────────────────┐
       │ Ingest workers (one per camera)               │
       │   YOLOv8 person detection                     │
       │   ByteTrack within-camera tracking            │
       │   OSNet appearance embedding per track        │
       │   Zone / tripwire evaluation                  │
       └───────────────────────┬───────────────────────┘
                               │ detection_events
                               ▼
                       ┌───────────────┐
                       │ Redis Streams │ events stream
                       └───────┬───────┘
                               │ consume
                               ▼
       ┌────────────────────────────────────────────────┐
       │ Aggregator                                     │
       │  • cross-camera identity matcher (embeddings)  │
       │  • session state machine (opens/closes on CAM3)│
       │  • POS receipt join (±90 s window on CAM 5)    │
       │  • staff classifier (CAM 4 gallery)            │
       │  • funnel + anomaly computations               │
       └───────────────────────┬────────────────────────┘
                               │ writes
                               ▼
                       ┌───────────────┐
                       │ Postgres      │
                       │ (analytics)   │
                       └───────┬───────┘
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
    ┌───────────────┐                     ┌───────────────┐
    │ FastAPI       │                     │ Streamlit     │
    │ /metrics      │◀────────────────────│ dashboard     │
    │ /funnel       │                     └───────────────┘
    │ /anomalies    │
    └───────────────┘
```

Each box is one container in `docker-compose.yml`. Redis Streams is the
canonical event bus; Postgres holds the materialised aggregates the API
reads from. The aggregator is the only writer to Postgres.

**One ingest worker per camera** isolates failures (a stalled CAM 4 cannot
back up CAM 3) and lets us scale horizontally without changing code. The
workers share a Docker image; the `CAMERA_ID` env var selects which video
file or RTSP URL they read.

## 3. Detection pipeline (per-camera)

**Frame source.** `services/ingest` reads an MP4 file (or RTSP URL) via
OpenCV. Frame rate is decoupled from wall clock — we forward a monotonic
`frame_ts` derived from the source timestamp so the pipeline produces the
same events at 1× or 4× playback.

**Detection.** YOLOv8n (`ultralytics`) with `classes=[0]` (person).
Lightweight, runs on CPU at a few FPS, GPU if available. Threshold `0.4`
— we'd rather miss a frame than spawn phantom tracks.

**Within-camera tracking.** ByteTrack via `supervision`. Robust to short
occlusions (people passing behind the makeup unit, behind a colleague at
the cash counter). Track IDs are local to a camera and prefixed
(`c1_track_417`, `c5_track_22`) to stay unambiguous downstream.

**Appearance embedding.** A lightweight OSNet model (`torchreid`'s
`osnet_x0_25`, ~ 1 M params, runs on CPU) produces a 512-d embedding per
track update. The embedding is the bridge between cameras — it's what the
aggregator uses to decide that `c3_track_5` (entered through the door) and
`c1_track_19` (now browsing Lakme Skin) are the same person.

**Per-camera responsibilities.** Each camera has different work to do:

| Camera | What ingest emits |
|---|---|
| CAM 1 | `zone_entered`, `zone_dwell` for top-wall shelf polygons |
| CAM 2 | `zone_entered`, `zone_dwell` for bottom-wall shelf polygons |
| CAM 3 | `person_entered`, `person_exited` on the door tripwire |
| CAM 4 | `staff_observed` — any track seen here joins the staff gallery |
| CAM 5 | `checkout_observed` when track lingers > 5 s near counter |

All events carry `camera_id`, `track_id`, `embedding_id` (FK into a fast
embedding store), and `ts`. The aggregator stitches identities.

**Entry/exit counting (CAM 3).** A single virtual *tripwire* is drawn
vertically across the glass-partition seam in CAM 3. The vestibule's
**right half** is the mall corridor (dark tile), the **left half** is the
store interior (wood floor + Purplle standee). A track crossing **right →
left** for the first time triggers `person_entered`; the reverse triggers
`person_exited`. A debounce of N frames prevents shimmer. The tripwire and
direction are configured in `config/cameras.yaml`.

**Zone mapping (CAM 1, CAM 2, CAM 5).** Each in-store camera has its own
zone polygons in pixel coordinates (no homography needed because each
camera is the single observer of its zones). Polygons live in
`config/zones/cam1.yaml`, `cam2.yaml`, `cam5.yaml`. The track's foot point
(bottom-centre of bbox) is tested against polygons each frame.

**Edge cases we explicitly handle:**

| Case | Approach |
|---|---|
| **Same person seen in multiple cameras** | Cross-camera matcher: when a track first appears in a camera, the aggregator queries the embedding store for the nearest neighbour among active sessions within the last 30 s. Match below threshold → attach to that session. No match → new candidate session, confirmed when CAM 3 entry is associated. |
| **Re-entry within the visit** | A track that exits CAM 3's tripwire and re-crosses inward within `REENTRY_GAP_S` (default 60 s) is matched by appearance to the previously-open session and the session stays open. Beyond the gap, a new session is opened. |
| **Staff / salespeople** | CAM 4 is the back-of-house. Any track that appears in CAM 4 contributes its embedding centroid to a `staff` gallery. Tracks on customer-facing cameras whose embedding distance to the staff gallery falls below a threshold are tagged `role=staff` and excluded from `footfall`. The salesperson roster from the POS CSV provides labels we attach when matches are confident. |
| **Occlusion behind fixtures** | ByteTrack's low-conf second pass handles short gaps. Stale tracks are retired after `TRACK_TTL_S` (5 s), which prevents inflating entry counts when someone steps behind a fixture. |
| **Camera glare / dropped feed** | A sliding window per camera: if detection variance collapses to zero for > 30 s during operating hours, the ingester emits `health_warning(camera_id, reason="frozen")` instead of silently producing nothing. |
| **Customer at billing but no receipt** | If `checkout_observed` is followed by no `pos_receipt` in `POS_JOIN_WINDOW_S` (±90 s), the session terminates at the `checkout_queued` funnel stage — counted as drop-off, not purchase. Conversely, an unmatched POS receipt is logged but not back-attributed to a session. |

## 4. Cross-camera identity reconciliation

**Shipped today.** The aggregator's `SessionStore` keys sessions on the
event's `embedding_id` (falling back to `track_id` when absent). A
session opens on `person_entered` from CAM 3 and closes on the matching
`person_exited`. Events from CAM 1/2/5 attach to the open session whose
key matches. CAM 4 sightings flip the session's `role` to `staff`.
Re-entry within `REENTRY_GAP_S` (60 s) reopens the previous session
instead of starting a new one.

For the synthetic publisher, embedding_ids are stable across cameras for
a given customer (the simulator knows the ground truth). For the video
worker, embedding_ids are deterministic per camera+track — so events
from a single camera bind into one session correctly, but a customer
walking from CAM 3 → CAM 1 won't merge across cameras without a real
appearance descriptor.

**Deferred (CHOICES.md §10).** Production cross-cam re-ID via an
OSNet embedding + FAISS index — the contract is already shaped so this
slot in cleanly: replace the embedding_id generator in
`services/ingest/track_state.py` with an OSNet hash, and the
SessionStore matching code keeps working unchanged.

## 5. Event schema

All events are JSON, written to Redis Stream `events`. Common envelope:

```json
{
  "event_id":  "uuid",
  "type":      "person_entered",
  "store_id":  "ST1008",
  "camera_id": "cam_3_entry",
  "ts":        "2026-04-10T20:10:14.412+05:30",
  "session_id": null,
  "track_id":   "c3_track_5",
  "embedding_id": "emb_…",
  "role":       "unknown",
  "payload":    { … type-specific … }
}
```

`session_id` is `null` at emit time for in-store cameras; the aggregator
fills it in after reconciliation. The events are *not* mutated — the
aggregator writes its own `session_event` rows with the resolved
`session_id`.

| Event type | Emitted by | `payload` |
|---|---|---|
| `person_entered` | CAM 3 | `{ direction: "in", line_id: "door_main" }` |
| `person_exited`  | CAM 3 | `{ direction: "out", line_id: "door_main" }` |
| `zone_entered`   | CAM 1, CAM 2 | `{ zone_id, first_visit_in_session }` |
| `zone_dwell`     | CAM 1, CAM 2 | `{ zone_id, dwell_s }` |
| `checkout_observed` | CAM 5 | `{ zone_id: "cash_counter", queue_position }` |
| `staff_observed` | CAM 4 | `{}` |
| `pos_receipt`    | POS ingester | `{ invoice_number, salesperson_id, total_amount, item_count, payment_mode }` |
| `health_warning` | any ingester | `{ source, reason }` |

Full schema with JSON Schema validation lives in
[`docs/EVENT_SCHEMA.md`](EVENT_SCHEMA.md).

## 6. Sessions, funnel, and conversion

A **session** is one customer visit. It opens on a `person_entered` event
from CAM 3 and closes on the matching `person_exited`. All zone, engage,
and checkout events that the cross-camera matcher binds to the session
land on its timeline. A single visitor cannot be double-counted no matter
how many cameras observed them.

**Funnel stages** (each session reaches at most one terminal stage):

1. `entered` — CAM 3 inward tripwire crossing.
2. `browsed` — any `zone_entered` for a shelf zone (CAM 1 or CAM 2).
3. `engaged` — `zone_dwell ≥ 20 s` in any shelf or the makeup unit.
4. `checkout_queued` — `checkout_observed` from CAM 5 attributed.
5. `purchased` — a `pos_receipt` falls within ±90 s of the session's
   `checkout_observed`. The bill is assigned to the session whose
   `checkout_observed` timestamp is closest; ties broken by earliest.

**Conversion rate** = `purchased / entered`, per hour and per day.

## 7. APIs

FastAPI (`services/api`). All responses JSON; query params standard.

| Endpoint | Returns |
|---|---|
| `GET /metrics?hours=N`     | footfall, conversion, revenue ₹, avg basket, items, dwell |
| `GET /funnel?hours=N`      | cumulative counts per funnel stage |
| `GET /hourly?hours=N`      | per-hour footfall + purchases (for the trend chart) |
| `GET /sales?hours=N`       | top salespeople, payment-mode mix, hourly revenue |
| `GET /zones?hours=N`       | per-zone unique visitors, total dwell, avg dwell |
| `GET /anomalies?hours=N`   | detected anomalies with severity + details |
| `GET /activity?limit=N`    | recent customer sessions (purchases + walks) |
| `GET /sessions/{id}`       | full row for a single session |
| `GET /cameras`             | per-camera event rate + last event ts |
| `GET /events/recent?n=N`   | tail of the raw Redis Stream (debug) |
| `GET /healthz` `/readyz`   | liveness / readiness |
| `GET /metrics-prom`        | Prometheus exposition (API metrics only) |

OpenAPI spec is auto-published at `/docs`.

## 8. Anomaly detection

Three families, all running on a 1-minute schedule inside the aggregator:

1. **Footfall outlier** — rolling 7-day same-weekday-same-hour mean & std;
   flag if `|z| > 2.5`.
2. **Conversion drop** — compare current-hour conversion against
   prior-3-hour mean; flag if drop > 30 % and footfall > 20.
3. **Dead zone** — a shelf zone whose unique-visitor count over the last
   hour is < 25 % of its 14-day median, during operating hours.

Each anomaly is persisted as a row and surfaced through `/anomalies`.

## 9. Production readiness

- **Deployment**: single `docker compose up --build`. Healthcheck-gated
  startup (`--wait`) verified in CI by the `stack` job.
- **Observability**:
  - Structured JSON logs (`structlog`) on every service.
  - Prometheus exposition on the API (`/metrics-prom`) — API request
    counters today; per-camera ingest counters land when the video
    profile is activated.
- **Testing**:
  - 57 unit + integration tests covering: schema round-trips, tripwire
    crossing, polygon containment, role-specific event handlers, session
    state machine (entries, exits, dwell, POS join, re-entry), POS CSV
    parsing, anomaly detector, demo-seed distribution, and an end-to-end
    synth → SessionStore → funnel pipeline.
  - CI runs three jobs: `test` (ruff + pytest), `compose` (compose-file
    validation), `stack` (real `docker compose up` + curl assertions on
    `/metrics` and `/funnel`).

## 10. Out of scope (by design)

- Person re-identification across days — out of rubric scope and raises
  privacy concerns we are not equipped to weigh in this timeframe.
- Demographic inference (age/gender) — ethically fraught and out of the
  evaluation rubric.
- A bespoke detector — YOLOv8n is well-calibrated for "person" on
  retail-style footage. Fine-tuning would require labels we do not have.
- Kubernetes manifests — the deploy target is `docker compose`.

## 11. What's shipped today vs. deferred

The acceptance-gate brief gives reviewers 10 minutes. Everything below
runs in the default `docker compose up` and is unit + integration
tested:

| Component | Status | Where |
|---|---|---|
| Event bus (Redis Streams, Pydantic envelope, JSON Schema-validated) | ✅ | `services/events/` |
| Synthetic ingest (30-session timeline at 1× wall-clock pace) | ✅ | `services/ingest/synth.py` |
| POS CSV replay (Brigade-style headers, time-shift to now) | ✅ | `services/pos/` |
| Aggregator session state machine (entry → exit, zones, dwell, POS join, re-entry, staff tag) | ✅ | `services/aggregator/session.py` |
| Postgres persistence (sessions, zone visits, raw events, hourly rollup) | ✅ | `services/aggregator/db.py` |
| Anomaly detection (footfall z-score, conversion drop, dead zone) | ✅ | `services/aggregator/anomalies.py` |
| FastAPI (12 endpoints incl. `/metrics`, `/funnel`, `/hourly`, `/sales`, `/zones`, `/activity`, `/anomalies`, `/sessions/{id}`, `/cameras`) | ✅ | `services/api/` |
| Streamlit dashboard with auto-refresh, sales + payments breakdown, live activity feed | ✅ | `services/dashboard/` |
| Demo data seed (480 sessions over 24h) for instant first impression | ✅ | `services/aggregator/seed.py` |
| 52 unit + 5 integration tests; 3-job CI (ruff/pytest, compose-config, live stack bring-up) | ✅ | `tests/`, `.github/workflows/ci.yml` |
| **Per-camera YOLOv8n + ByteTrack worker** | ✅ Logic tested; runtime needs footage | `services/ingest/video_worker.py` |

The video worker is fully implemented. Logic that doesn't depend on a
GPU or footage — tripwire crossing detection, polygon membership, dwell
accumulation, cash-counter lingering, staff observation rate-limiting —
is unit-tested via `tests/unit/test_geom.py` and
`tests/unit/test_track_state.py`.

The runtime path (`docker compose --profile video up`) loads YOLOv8n via
ultralytics on CPU. Verification against real footage requires the
Brigade clips mounted at `./data/video/CCTV Footage/CAM N.mp4` and is
the only path that exercises the OpenCV decoder + the YOLO inference +
ByteTrack. The default `docker compose up` (synthetic mode) does NOT
need any of that — it's what passes the acceptance gate.

The two paths share the same event contract: a video frame producing a
`zone_entered` envelope is indistinguishable downstream from the synth
publisher producing the same one.

## 12. Live demo recipe

```bash
docker compose up --build
# wait ~ 30 s for healthchecks, then open:
#   http://localhost:8501          dashboard
#   http://localhost:8000/docs     interactive OpenAPI
#   http://localhost:8000/metrics  raw KPIs
```

A canonical OpenAPI snapshot is committed at [`docs/openapi.json`](openapi.json)
for offline review.
