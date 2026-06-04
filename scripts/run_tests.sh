#!/usr/bin/env bash
# Run the repository tests in a local Python environment.
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 [unit|integration|all]

Arguments:
  unit         Run unit tests only
  integration  Run integration tests only
  all          Run both unit and integration tests (default)
EOF
}

MODE=${1:-all}

case "$MODE" in
  unit)
    pytest tests/unit -q
    ;;
  integration)
    pytest tests/integration -q
    ;;
  all)
    pytest -q
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    usage
    exit 2
    ;;
esac
