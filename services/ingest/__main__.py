"""Ingest entrypoint.

Behaviour depends on INGEST_MODE:

* `synthetic` (default): runs `services.ingest.synth` — no video, no model.
  The per-camera workers exit cleanly so `docker compose up` is verifiable
  on any machine.
* `video`: per-camera YOLO + ByteTrack worker (selected by CAMERA_ID).
  Implemented in the next slice; for now logs a clear "not yet implemented"
  message and exits with code 2 so docker-compose surfaces it.
"""

from __future__ import annotations

import asyncio
import os
import sys


def main() -> int:
    mode = os.environ.get("INGEST_MODE", "synthetic").lower()
    if mode == "synthetic":
        from services.ingest.synth import _main as synth_main

        asyncio.run(synth_main())
        return 0
    if mode == "video":
        camera_id = os.environ.get("CAMERA_ID", "<unset>")
        print(
            f"[ingest] INGEST_MODE=video selected but the per-camera worker "
            f"is not yet implemented (CAMERA_ID={camera_id}). "
            "See PR series — slice 2.5 wires YOLO + ByteTrack here.",
            file=sys.stderr,
        )
        return 2
    print(f"[ingest] Unknown INGEST_MODE={mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
