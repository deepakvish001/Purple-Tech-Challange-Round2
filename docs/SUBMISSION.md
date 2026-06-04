# Submission checklist — Purplle Tech Challenge 2026 (Round 2)

This file documents what to include when uploading the repository to GitHub
for the Purplle Tech Challenge submission. Follow the checklist below to
ensure reviewers can run and evaluate your solution without requiring the
raw CCTV or full POS datasets.

- **Include**:
  - `README.md` with quick start and useful URLs
  - `docs/` containing DESIGN.md, CHOICES.md, EVENT_SCHEMA.md, and any
    architecture diagrams or evaluation notes
  - `services/` application code (ingest, aggregator, api, dashboard, pos)
  - `infra/` and `docker-compose.yml` for runnable stack
  - `scripts/` helpers used to boot the stack and run tests
  - `tests/` (unit and integration) and small fixtures under `tests/fixtures/`
  - `pyproject.toml` and any lockfiles describing dependencies

- **Exclude (do not commit)**:
  - Raw CCTV video archives and full POS export CSVs — these must be
    mounted locally under `./data/` and are intentionally gitignored
  - Large binary datasets, model checkpoints, or any private credentials

- **Submission notes for reviewers**:
  - Explain how the synthetic simulator is used (see `services/ingest/synth.py`).
  - Point reviewers to `./scripts/start.sh` / `./scripts/start.ps1` for a one-command
    way to run the system locally.
  - Describe how to run tests: `./scripts/run_tests.sh all` and how to run
    integration tests with Docker.

- **Optional extras to include** (helpful but not required):
  - A short `SUBMISSION_DEMO.md` or screencast link showing the dashboard and
    key API endpoints in action
  - A tiny sample of redacted POS rows or a 1–2 second video clip in
    `tests/fixtures/` that demonstrates the pipeline (keep these tiny)

## How to prepare the GitHub repo

1. Run the test suite locally and ensure `pytest` passes.
2. Remove any accidental commits of large files. Use `.gitignore` to keep
   `./data/` out of the repository.
3. Tag the repo with a release (optional) and include a short description
   in the release notes describing system assumptions and how to reproduce
   the demo.

Good luck — ship something you're proud of.
