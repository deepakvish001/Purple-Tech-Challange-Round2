#!/usr/bin/env bash
# Start the full stack for local development (Linux / macOS)
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 [profile]

Profiles:
  default   Start the default stack (synthetic ingest, aggregator, api, dashboard)
  video     Include the video profile (replay from ./data/video)
  full      Include Prometheus scraper as well

Examples:
  $0
  $0 video
  $0 full
EOF
}

PROFILE=${1:-default}

case "$PROFILE" in
  default)
    docker compose up --build
    ;;
  video)
    docker compose --profile video up --build
    ;;
  full)
    docker compose --profile full up --build
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    echo "Unknown profile: $PROFILE" >&2
    usage
    exit 2
    ;;
esac
