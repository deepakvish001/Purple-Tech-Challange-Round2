# Purpleye — Real-time Retail Intelligence System

End-to-end pipeline that turns raw in-store CCTV footage and POS receipts into business-relevant retail metrics: footfall, zone engagement, funnel conversion, anomalies, and a production-shaped API + live dashboard.

Built for the Purplle Tech Challenge 2026 — Round 2.
- Challenge page: https://www.hackerearth.com/community/challenges/hackathon/purplle-tech-challenge-2026-round-2/
- Problem statement, sample POS, and sample event resources are treated as external inputs.

## What this repo contains

- `services/` — ingest, event streaming, aggregator, API, dashboard, POS publisher
- `infra/` — Dockerfiles and Prometheus scrape config
- `docs/` — architecture, design decisions, event schema
- `tests/` — unit and integration validation
- `scripts/` — helper scripts for startup and test execution

## Why this is arranged this way

This repo is organized for a production-style submission:
- application code lives under `services/`
- runtime config lives under `infra/` and `docker-compose.yml`
- documentation lives under `docs/`
- tests and CI fixtures are isolated under `tests/`
- raw CCTV / POS source data is explicitly excluded from version control

## Quick start

### Start the default stack

```bash
docker compose up --build
```

Default stack: Redis + Postgres + synthetic ingest + POS + aggregator + FastAPI + Streamlit dashboard.

### Start with helper scripts

- Linux / macOS: `./scripts/start.sh`
- Windows PowerShell: `.\scripts\start.ps1`

Profiles:
- `default` — synthetic ingest, aggregator, API, dashboard
- `video` — include the `video` profile to replay local clips from `./data/video/`
- `full` — include Prometheus scraping and observability pieces

Examples:

```bash
./scripts/start.sh
./scripts/start.sh video
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Profile video
```

### Useful URLs

- Dashboard: <http://localhost:8501>
- API docs: <http://localhost:8000/docs>
- Metrics: <http://localhost:8000/metrics>
- Funnel: <http://localhost:8000/funnel>
- Sales breakdown: <http://localhost:8000/sales>
- Hourly trend: <http://localhost:8000/hourly>
- Activity feed: <http://localhost:8000/activity>
- Recent events: <http://localhost:8000/events/recent?n=20>
- Prometheus: <http://localhost:8000/metrics-prom>

### Replay raw video locally

Drop clips into `./data/video/` and run:

```bash
docker compose --profile video up --build
```

### Add Prometheus scraping

```bash
docker compose --profile full up --build
```

## Local development

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -e ".[dev]"
```

Run a quick local check:

```bash
pytest -q
ruff check services tests
```

Regenerate the committed OpenAPI snapshot:

```bash
PYTHONPATH=. python scripts/dump_openapi.py
```

## Tests & benchmarks

Use the helper scripts in `scripts/` to run tests consistently.

### Run tests

```bash
./scripts/run_tests.sh all
./scripts/run_tests.sh unit
./scripts/run_tests.sh integration
```

PowerShell:

```powershell
.\scripts\run_tests.ps1 -Mode all
```

### Slow tests and profiling

```bash
pytest --durations=10
```

### Benchmarking

Install `pytest-benchmark` and save a run:

```bash
pip install pytest-benchmark
pytest --benchmark-save=run1
pytest --benchmark-compare
```

### Capture test output

```bash
pytest -q --durations=10 > test_report.txt
```

### Simulation details

The project includes a deterministic synthetic event publisher at `services/ingest/synth.py`. It builds a pure timeline with `generate_timeline()` and publishes events at configurable pace through `SYNTH_PACE`. In tests and CI, the simulator runs with `SYNTH_PACE=0` so timelines are deterministic and fast.

## Repository layout

```
services/
  ingest/        # video reader + zone mapper + synthetic event generator
  events/        # event schema + Redis stream publisher/subscriber
  api/           # FastAPI app and analytics endpoints
  aggregator/    # session joiner, funnel, anomalies, and materialised views
  dashboard/     # Streamlit live UI
  pos/           # POS CSV loader + receipt publisher
infra/
  docker/        # Dockerfiles for each service
  prometheus/    # Prometheus scrape configuration
scripts/        # startup and test helpers
docs/           # design, architecture, and event schema docs
tests/          # unit and integration coverage
docker-compose.yml
```

## Submission notes

This repository follows the challenge requirement not to include raw CCTV footage or full POS export data in source control. Those inputs should be mounted locally under `./data/` when available. The repo ships application code, configuration, docs, and small test fixtures only.

See [docs/SUBMISSION.md](docs/SUBMISSION.md) for a short checklist and reviewer guidance when creating your GitHub submission.

## Not included in this repo

Per the challenge instructions, the raw CCTV archive and POS exports are **not** committed. Place them under `./data/` locally; the directory is gitignored. A tiny fixture clip + synthetic POS rows under `tests/fixtures/` keep CI runnable.
