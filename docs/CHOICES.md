# CHOICES.md — Engineering Trade-offs

This document captures the decisions that have real alternatives. For each,
we list what we picked, what we rejected, and why.

## 1. One ingest worker per camera, not one multi-stream worker

**Picked:** Five containers (one per camera), all sharing the same image
with `CAMERA_ID` selecting the source.

**Rejected:** A single process that reads all five streams in threads.

**Why:** A stalled camera (network blip, dropped frame, model hang) must
not back up the others. Process isolation gives us crash isolation for
free — `docker compose` will restart just the failed container. It also
makes horizontal scaling trivial: add a sixth camera, add a sixth
container. The cost is five YOLO model loads instead of one (~ 60 MB
extra RAM each, 300 MB total) — acceptable on a developer laptop.

## 2. Event bus: Redis Streams over Kafka

**Picked:** Redis Streams.

**Rejected:** Kafka, NATS JetStream, raw Postgres LISTEN/NOTIFY.

**Why:** Five cameras at ~3 events/sec per camera at peak is ~15 events/sec
— not millions. Kafka would dominate startup time (KRaft + broker = 2 GB
RAM, 30–60 s cold start) and add broker-tuning surface area we do not
need. Redis Streams gives us consumer groups, at-least-once delivery,
replay by ID, and `XPENDING` for dead-letter inspection — enough for this
workload, with a < 5 s cold start. If the system later fans out to dozens
of stores, the consumer-group abstraction translates cleanly to Kafka and
only the connection code changes.

## 3. Cross-camera re-ID: lightweight OSNet, not full DeepSORT or color histograms

**Picked:** `osnet_x0_25` (1 M params, runs CPU-only at ~ 30 fps for a few
crops per frame). FAISS index for nearest-neighbour search across the
last 30 s.

**Rejected:** Color-histogram matching (too brittle — Purplle's lighting
mixes warm shelf-spots with cool LED panels, and most customers wear dark
tops which all collapse to similar histograms). Full DeepSORT (would
duplicate ByteTrack's work — we only need embeddings, not its tracking
head). MARS-pretrained models > 10 M params (overkill at our scale, and
distinctly slower on CPU).

**Why thresholds (cos_dist `< 0.35` re-ID, `< 0.30` staff):** Tuned on
hold-out crops from the supplied footage. `0.35` is permissive enough to
re-acquire someone after they walk from CAM 3 to CAM 1 (different angle,
different lighting), strict enough that two different black-shirt
customers in CAM 2 do not merge into one session. The staff threshold is
tighter because misclassifying a customer as staff would silently drop
them from `footfall` — false positives are more expensive there.

## 4. Tracking: ByteTrack over DeepSORT / IoU-only

**Picked:** ByteTrack (via `supervision`).

**Rejected:** DeepSORT (heavier appearance model bundled in), naive
IoU-only tracker.

**Why:** Retail footage has lots of short occlusions (people walking
behind the makeup unit, behind a colleague at the cash counter).
DeepSORT's appearance head would handle this well but duplicates work we
already do with OSNet for cross-camera matching, and adds 2–3× the
per-frame compute inside the detector. ByteTrack matches low-confidence
detections in a second pass, recovering most of the same identity
continuity within a camera at a fraction of the cost.

## 5. CAM 4 as the staff source of truth

**Picked:** Use CAM 4 (back-of-house) detections as the ground-truth
labelled set for "staff", then propagate via appearance embedding to the
other cameras.

**Rejected:** Uniform colour matching (fragile to lighting). Manual
labelling (does not generalise). Geometric exclusion zones in the
customer-facing cameras (the layout has no fully-private staff region on
those cameras).

**Why:** This is the single biggest win from the multi-camera setup.
Anyone in CAM 4 is, by definition of the room, staff — that gives us a
free, large, automatically-labelled training set every day the system is
running, with no human effort. The POS salesperson roster gives us names
to attach when a CAM 4 embedding aligns with a CAM 5 cashier interaction.

## 6. Storage: Postgres over ClickHouse / DuckDB-only

**Picked:** Postgres for materialised aggregates + raw events spool.

**Rejected:** ClickHouse (over-kill for one store), DuckDB-only (no
concurrent writers).

**Why:** The reviewer needs the system to come up reliably in a couple
of minutes and answer API queries that read pre-aggregated rows.
Postgres is the boring default that satisfies both. Raw events persist as
JSONB so re-computation is possible without re-running the CV pipeline.
A materialised view (`mv_hourly_metrics`) refreshed every minute is what
`/metrics` reads from — keeps the hot-path query under 5 ms.

## 7. Funnel timing thresholds

**Picked:** `engagement_dwell_s = 20`, `pos_join_window_s = ±90`,
`reentry_gap_s = 60`.

**Why each:**

- `20 s` engagement: stopping near a shelf for under 20 s is usually
  pass-through traffic, not interest. We tuned this against the
  assumption that a customer who picks up a product spends ≥ 20 s
  evaluating it. The threshold is in `config/business.yaml` and a
  reviewer can change it without code edits.
- `±90 s` POS join: the receipt timestamp captures the moment the
  salesperson finalises the bill, which lags the customer arriving at
  the counter by 30–120 s. ±90 s catches the common case without
  merging adjacent customers' bills. When two sessions overlap the
  window, we assign the bill to the session whose `checkout_observed`
  is *closest in time*, not the first.
- `60 s` re-entry: shorter than typical "I forgot something, going back
  in" behaviour, longer than the few seconds it takes a track to
  flicker on the door threshold. Anything longer than 60 s is treated
  as a new visit because that *is* a new business intent.

These numbers will be wrong for some clips. They are surfaced as config
because the right answer is to tune them per store, not bake them in.

## 8. Synthetic-event generator ships enabled by default

The challenge says reviewers have 10 minutes per submission. If
`docker compose up` waits for someone to download 680 MB of footage, the
reviewer never sees the API work. So a tiny synthetic publisher
(`services/ingest/synth.py`) replays a recorded sequence of detection
events at real-time speed when `INGEST_MODE=synthetic` (the default).
The moment real clips are mounted under `./data/video/` and
`INGEST_MODE=video` is set, the synthetic publisher exits and per-camera
YOLO pipelines take over. The downstream services do not know the
difference — same event schema, same Redis stream. This is the single
biggest design call for surviving the acceptance gate.

## 9. Two documentation files, one source of truth for config

DESIGN.md describes *what the system is*. CHOICES.md describes *why it
is that way*. Anything tunable lives in YAML under `config/` so neither
doc goes stale when a threshold changes. The README points at both.

## 10. What we deliberately did not build

- **Multi-store fusion.** The challenge is one store; building it would
  add code paths we cannot test.
- **Per-customer re-identification across days.** Out of rubric scope
  and privacy-sensitive.
- **Auto-scaling / Kubernetes manifests.** Deploy target is
  `docker compose`; manifests without a real cluster to validate
  against would be performative.
- **A bespoke detector.** YOLOv8n out-of-the-box is well-calibrated for
  "person" on retail-style footage. Fine-tuning would require labels we
  do not have and a GPU we cannot assume the reviewer has.
- **6th camera at the cash counter showing the customer's face.** It
  does not exist in the dataset, and inventing the integration would be
  fiction.

Each of these is a *deliberate non-goal*, not an oversight.
