# Store Intelligence System — Brigade Road, Bangalore

End-to-end pipeline that turns raw in-store CCTV footage (five cameras) +
POS receipts into business-relevant retail metrics (footfall, zone
engagement, funnel, conversion, anomalies) and exposes them through a
production-shaped API and a live dashboard.

Built for the Purplle Tech Challenge 2026 — Round 2. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the system architecture and
[`docs/CHOICES.md`](docs/CHOICES.md) for the engineering trade-offs.

## What it does

1. Ingests five CCTV streams in parallel (one worker per camera): top-wall
   shelves, bottom-wall shelves, entry vestibule, back-of-house, cash
   counter.
2. Runs per-camera YOLOv8 person detection + ByteTrack tracking, and
   emits OSNet appearance embeddings used for cross-camera identity
   reconciliation.
3. Emits structured events (`person_entered`, `person_exited`,
   `zone_entered`, `zone_dwell`, `checkout_observed`, `staff_observed`)
   onto a Redis stream.
4. A stateful aggregator stitches identities across cameras into
   sessions, joins them with POS receipts, and computes funnel +
   anomalies.
5. A FastAPI service exposes `/metrics`, `/funnel`, `/anomalies`,
   `/zones`, `/sessions`, and `/cameras`. A Streamlit dashboard renders
   them live.

## Run

```bash
docker compose up --build
```

Then:

- API: <http://localhost:8000/docs>
- Dashboard: <http://localhost:8501>
- Prometheus: <http://localhost:8000/metrics-prom>

A small synthetic event generator runs by default so the system is verifiable
without the (large) CCTV archive. To replay real footage, drop clips into
`./data/video/` and set `INGEST_MODE=video` in `.env`.

## Repository layout

```
services/
  ingest/        # video reader + YOLO + ByteTrack + zone mapper
  events/        # event schema + Redis stream client
  api/           # FastAPI app, metrics & funnel endpoints
  aggregator/    # stateful joiner: detection events + POS receipts
  dashboard/     # Streamlit UI
  pos/           # POS CSV loader + receipt event publisher
infra/
  docker/        # Dockerfiles per service
  prometheus/    # scrape config
docs/
  DESIGN.md
  CHOICES.md
  EVENT_SCHEMA.md
tests/
  unit/
  integration/
docker-compose.yml
```

## Not included in this repo

Per the challenge instructions, the raw CCTV archive and POS exports are
**not** committed. Place them under `./data/` locally; the directory is
gitignored. A tiny fixture clip + synthetic POS rows under
`tests/fixtures/` keep CI runnable.
