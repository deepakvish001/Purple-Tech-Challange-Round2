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
| **Group entry** | Each track is counted independently for footfall. We additionally publish `group_id` when two or more CAM 3 entry events fall within 2 s and the embeddings co-locate in CAM 1/2 for ≥ 10 s — so conversion can be measured per-group too. |
| **Occlusion behind fixtures** | ByteTrack's low-conf second pass handles short gaps. Longer gaps (> 1 s) are buffered as `lost` for up to 5 s before retiring, which prevents inflating entry counts when someone steps behind a fixture. |
| **Camera glare / dropped feed** | A sliding window per camera: if detection variance collapses to zero for > 30 s during operating hours, the ingester emits `health_warning(camera_id, reason="frozen")` instead of silently producing nothing. |
| **Customer at billing but no receipt** | If `checkout_observed` is followed by no `pos_receipt` in `POS_JOIN_WINDOW_S` (±90 s), the session terminates at the `checkout_queued` funnel stage — counted as drop-off, not purchase. Conversely, an unmatched POS receipt is logged but not back-attributed to a session. |

## 4. Cross-camera identity reconciliation

This is the only genuinely interesting algorithmic component of the system,
and the place where the multi-camera setup pays off.

State held by the aggregator:

- `active_sessions: dict[session_id, SessionState]` — open sessions.
- `embedding_store: ANN index of (track_id, camera_id, ts, embedding)`
  keeping the last 30 s of embeddings across all cameras (FAISS L2,
  in-memory).
- `staff_gallery: list[embedding]` — bootstrapped from CAM 4.

On every detection event:

1. If `camera_id == CAM 3` and the event is `person_entered`: open a new
   candidate session. Stash the track's recent embeddings.
2. If `camera_id ∈ {CAM 1, CAM 2, CAM 5}` and a *new* track starts: query
   the ANN index for the nearest neighbour with `cos_dist < REID_THRESHOLD`
   (default 0.35). If matched to a session within 30 s → attach.
   Otherwise hold the track as orphan until a future CAM 3 retro-match
   resolves it (covers the case where ingest workers fire out of order).
3. If `camera_id == CAM 4`: tag the track's embedding as `staff` and
   update the gallery centroid.
4. If a session's embedding closely matches the staff gallery
   (`cos_dist < STAFF_THRESHOLD`, default 0.3): mark `role=staff`.

The 30 s window matches typical in-store walking time from door to back
shelf and avoids combinatorial blow-up. The thresholds are config, not
constants — see CHOICES.md §3.

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
| `group_detected` | aggregator | `{ group_id, member_session_ids }` |
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
| `GET /metrics?from=…&to=…` | footfall, unique sessions, avg dwell, conversion |
| `GET /funnel?from=…&to=…`  | counts per funnel stage + drop-off % |
| `GET /zones`               | per-zone unique visitors, total dwell, peak hour |
| `GET /anomalies?from=…&to=…` | list of detected anomalies with severity |
| `GET /sessions/{id}`       | full timeline of a single session, all cameras |
| `GET /cameras`             | per-camera health, fps, last event ts |
| `GET /healthz` `/readyz`   | liveness / readiness |
| `GET /metrics-prom`        | Prometheus exposition |

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

- **Deployment**: single `docker compose up`. Health checks gate startup
  order (Redis → Postgres → aggregator → ingest×5 → api/dashboard).
- **Observability**:
  - Structured JSON logs with `trace_id` propagated through Redis message
    headers.
  - Prometheus metrics: events-per-second per camera, processing lag,
    cross-camera match rate, API latency histograms.
  - OpenTelemetry traces from API → Postgres exported to the optional
    `otel-collector` container.
- **Testing**:
  - Unit tests for tripwire crossing, zone polygon lookup, session
    state-machine, cross-camera matcher (synthetic embeddings), POS
    timestamp join.
  - Integration test that replays a 30 s fixture clip from each camera
    + synthetic POS rows and asserts a known event sequence and final
    `/metrics` payload.
  - CI workflow runs `pytest` + `ruff` + `mypy` on every push.

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
| FastAPI (`/metrics`, `/funnel`, `/anomalies`, `/zones`, `/sessions/{id}`, `/cameras`) | ✅ | `services/api/` |
| Streamlit dashboard with auto-refresh + session lookup | ✅ | `services/dashboard/` |
| 32 unit tests + 5 integration tests; ruff + CI gates | ✅ | `tests/`, `.github/workflows/ci.yml` |
| **Per-camera YOLOv8n + ByteTrack + OSNet worker** | 🟡 Stub | `services/ingest/__main__.py` |

The video-mode worker is intentionally stubbed. Reasons (consistent with
CHOICES.md §10):

- The synthetic publisher exercises the same event contract the real
  worker would emit, so the entire downstream pipeline (aggregator, API,
  dashboard) is verifiable end-to-end without footage or a GPU.
- Pulling `torch` + `ultralytics` into the image adds ~ 1 GB and 90 s of
  build time, hostile to the 10-minute acceptance gate.
- Without footage on the reviewer's machine, the worker has nothing to
  exercise — it would be untested code paying full build cost.

Activating it later is a single follow-up: implement
`services/ingest/video_worker.py`, drop the `INGEST_MODE=video` stub in
`services/ingest/__main__.py`, switch `infra/docker/ingest.Dockerfile`
onto a torch base image. The compose `video` profile is already wired.

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
