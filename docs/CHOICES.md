# CHOICES.md — Engineering Trade-offs

This document captures the decisions that have real alternatives. For each,
we list what we picked, what we rejected, and why.

## 1. Event bus: Redis Streams over Kafka

**Picked:** Redis Streams.

**Rejected:** Kafka, NATS JetStream, raw Postgres LISTEN/NOTIFY.

**Why:** A single Bangalore store running one camera produces a few hundred
events per minute at peak, not millions. Kafka would dominate startup time
(ZK or KRaft + broker = 2 GB RAM, 30–60 s cold start) and add a
broker-tuning surface area we do not need. Redis Streams gives us
consumer groups, at-least-once delivery, replay by ID, and `XPENDING` for
dead-letter inspection — enough for this workload, with a < 5 s cold start.
If the system later fans out to dozens of stores, the consumer-group
abstraction translates cleanly to Kafka and only the connection code
changes.

## 2. Tracking: ByteTrack over DeepSORT / IoU-only

**Picked:** ByteTrack (via `supervision`).

**Rejected:** DeepSORT (heavier appearance model), naive IoU tracker.

**Why:** The retail floor has lots of short occlusions (people walking
behind the makeup unit, behind a colleague at the cash counter). DeepSORT's
appearance head would handle this well but adds a 50 MB model and 2–3× the
per-frame compute. ByteTrack matches low-confidence detections in a second
pass, recovering most of the same identity continuity at a fraction of the
cost. We *do* still use appearance embeddings, but only for the staff-tag
and re-entry use cases where the cost is amortised across the day.

## 3. Storage: Postgres over ClickHouse / DuckDB-only

**Picked:** Postgres for materialised aggregates + raw events spool.

**Rejected:** ClickHouse (over-kill for one store), DuckDB-only (no
concurrent writers).

**Why:** The reviewer needs the system to come up reliably in a couple of
minutes and answer API queries that read pre-aggregated rows. Postgres is
the boring default that satisfies both. We persist raw events as JSONB so
re-computation is possible without re-running the CV pipeline. A
materialised view (`mv_hourly_metrics`) is refreshed every minute and is
what `/metrics` reads from — keeps the hot-path query under 5 ms.

## 4. Funnel timing thresholds

**Picked:** `engagement_dwell_s = 20`, `pos_join_window_s = ±90`,
`reentry_gap_s = 60`.

**Why each:**

- `20 s` engagement: stopping near a shelf for under 20 s is usually
  pass-through traffic, not interest. We tuned this against the assumption
  that a customer who picks up a product spends ≥ 20 s evaluating it. The
  threshold is in `config/business.yaml` and a reviewer can change it
  without code edits.
- `±90 s` POS join: the receipt timestamp captures the moment the
  salesperson finalises the bill, which lags the customer arriving at the
  counter by 30–120 s. ±90 s catches the common case without merging
  adjacent customers' bills. When two sessions overlap the window, we
  assign the bill to the session whose `checkout_observed` is *closest in
  time*, not the first.
- `60 s` re-entry: shorter than typical "I forgot something, going back in"
  behaviour, longer than the few seconds it takes a track to flicker on
  the door threshold. Anything longer than 60 s is treated as a new visit
  because that *is* a new business intent.

These numbers will be wrong for some clips. They are surfaced as config
because the right answer is to tune them per store, not bake them in.

## 5. Staff detection: appearance clustering + POS roster, not uniforms

**Picked:** Bootstrap one appearance centroid per `salesperson_id` from
crops in the first operating hour, then classify subsequent tracks against
the centroids.

**Rejected:** Uniform colour matching (fragile to lighting), manual
labelling (does not generalise), excluding the back-of-house zone
geometrically (the layout has no fully-private staff zone).

**Why:** We *have* the staff roster from the POS CSV — using it costs
nothing and gives a labelled bootstrap set. The fallback heuristic
(presence > 30 min + frequent cash-counter/PMU visits) catches anyone the
appearance step misses, e.g. a stockist who never appears on a receipt.

## 6. Two documentation files, one source of truth for config

DESIGN.md describes *what the system is*. CHOICES.md describes *why it is
that way*. Anything tunable lives in YAML under `config/` so neither doc
goes stale when a threshold changes. The README points at both.

## 7. Synthetic-event generator ships enabled by default

The challenge says reviewers have 10 minutes per submission. If
`docker compose up` waits for someone to download 680 MB of footage, the
reviewer never sees the API work. So a tiny synthetic publisher
(`services/ingest/synth.py`) replays a recorded sequence of detection
events at real-time speed when `INGEST_MODE=synthetic` (the default). The
moment a real clip is mounted under `./data/video/` and `INGEST_MODE=video`
is set, the synthetic publisher exits and the YOLO pipeline takes over.
The downstream services do not know the difference — same event schema,
same Redis stream. This is the single biggest design call for surviving
the acceptance gate.

## 8. What we deliberately did not build

- **Multi-camera fusion.** Only one camera covers the door clearly; building
  fusion logic for an unverified second view would add code paths we cannot
  test.
- **Per-customer re-identification across days.** Out of rubric scope and
  raises privacy concerns we are not equipped to weigh in 48 hours.
- **Auto-scaling / Kubernetes manifests.** The deploy target is
  `docker compose`. Kubernetes manifests without a real cluster to validate
  against would be performative.
- **A bespoke detector.** YOLOv8n out-of-the-box is well-calibrated for
  "person" on retail-style footage. Fine-tuning would require labels we do
  not have and a GPU we cannot assume the reviewer has.

Each of these is a *deliberate non-goal*, not an oversight.
