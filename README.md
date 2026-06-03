# Store Intelligence System — Brigade Road, Bangalore

End-to-end pipeline that turns raw in-store CCTV footage (five cameras) +
POS receipts into business-relevant retail metrics (footfall, zone
engagement, funnel, conversion, anomalies) and exposes them through a
production-shaped API and a live dashboard.

Built for the Purplle Tech Challenge 2026 — Round 2. See
[`docs/DESIGN.md`](docs/DESIGN.md) for the system architecture and
[`docs/CHOICES.md`](docs/CHOICES.md) for the engineering trade-offs.

## 30-second tour

1. **Five camera roles** — top-wall shelves (CAM 1), bottom-wall shelves
   (CAM 2), entry vestibule (CAM 3), back-of-house staff room (CAM 4),
   cash counter (CAM 5). Each gets its own ingest worker.
2. **Synthetic-first** — a deterministic event publisher emits realistic
   detection events so the entire pipeline (events → sessions →
   funnel → anomalies → API → dashboard) is verifiable in 60 s without
   the 680 MB footage archive.
3. **Aggregator** — a pure session state machine stitches detection
   events into visits, joins POS receipts within ±90 s of checkout, and
   classifies each session into a funnel stage. Persists to Postgres.
4. **API** — FastAPI surface (`/metrics`, `/funnel`, `/zones`,
   `/anomalies`, `/sessions/{id}`, `/cameras`) reads from materialised
   views the aggregator refreshes every 30 s.
5. **Dashboard** — Streamlit live UI auto-refreshes every 5 s.

See [`docs/DESIGN.md`](docs/DESIGN.md) §11 for the shipped-vs-deferred
status table.

## Run

```bash
docker compose up --build
```

Default stack: **Redis + Postgres + synthetic ingest + POS + aggregator +
FastAPI + Streamlit dashboard**. The synthetic publisher emits realistic
detection events so the dashboard is populated within seconds — no
footage download needed.

- **Dashboard**:    <http://localhost:8501>
- API docs:         <http://localhost:8000/docs>
- Live metrics:     <http://localhost:8000/metrics>
- Live funnel:      <http://localhost:8000/funnel>
- Sales breakdown:  <http://localhost:8000/sales>
- Hourly trend:     <http://localhost:8000/hourly>
- Activity feed:    <http://localhost:8000/activity>
- Recent events:    <http://localhost:8000/events/recent?n=20>
- Prometheus exp:   <http://localhost:8000/metrics-prom>

To replay real footage instead, drop clips into `./data/video/` and:

```bash
docker compose --profile video up --build
```

To add the Prometheus scraper:

```bash
docker compose --profile full up --build
```

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check services tests
```

Regenerate the committed OpenAPI snapshot:

```bash
PYTHONPATH=. python scripts/dump_openapi.py
```

## Sanity checks reviewers can run

```bash
# 1. Is the stack up?
curl localhost:8000/healthz

# 2. Are events flowing through Redis?
curl 'localhost:8000/events/recent?n=5' | jq '.events[0]'

# 3. Is the aggregator producing real metrics?
curl localhost:8000/metrics | jq

# 4. Funnel shape (should monotonically decrease)
curl localhost:8000/funnel | jq '.stages'

# 5. Zone heat
curl localhost:8000/zones | jq '.zones'

# 6. Pick a session id from the events feed and inspect it
curl localhost:8000/sessions/<uuid> | jq
```

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
