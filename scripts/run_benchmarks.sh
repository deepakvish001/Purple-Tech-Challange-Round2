#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 [save-name]

Runs benchmark tests (requires pytest-benchmark).
Optional `save-name` stores the benchmark run for later comparison.
EOF
}

SAVE=${1:-}

if ! command -v pytest >/dev/null 2>&1; then
  echo "pytest not found; ensure your virtualenv is active and pytest is installed" >&2
  exit 2
fi

if ! python -c "import pytest_benchmark" >/dev/null 2>&1; then
  echo "pytest-benchmark not installed. Install with: pip install pytest-benchmark" >&2
  exit 2
fi

if [ -n "$SAVE" ]; then
  pytest tests/benchmarks -q --benchmark-save="$SAVE"
else
  pytest tests/benchmarks -q
fi
