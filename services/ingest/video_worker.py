"""Per-camera video worker: YOLOv8n + ByteTrack + role-specific dispatch.

This is the runtime end of the contract that the synth publisher prototypes.
Same events, same envelope, same downstream pipeline — the only difference
is the source of truth (a real frame vs. a scripted timeline).

Heavy deps (torch + ultralytics + opencv + supervision) are imported
lazily so the lightweight ingest image (used by `INGEST_MODE=synthetic`)
doesn't pay for them.

Verified end-to-end against synthetic events; verification against real
footage requires a clip mounted at `/data/video/CCTV Footage/CAM N.mp4`
and is documented in DESIGN.md §11.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from services.events import EventBus
from services.ingest.config import CameraCfg, StoreCfg, load_store_cfg
from services.ingest.geom import foot_point
from services.ingest.track_state import make_handler

log = structlog.get_logger()

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CONF_THRESHOLD = float(os.environ.get("YOLO_CONF", "0.4"))
MODEL_NAME = os.environ.get("YOLO_MODEL", "yolov8n.pt")
VIDEO_ROOT = Path(os.environ.get("VIDEO_ROOT", "/data/video"))
LOOP_VIDEO = os.environ.get("LOOP_VIDEO", "true").lower() == "true"
FRAME_STRIDE = int(os.environ.get("FRAME_STRIDE", "3"))  # process every Nth frame


def _setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    structlog.configure(processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])


def _resolve_source(cam: CameraCfg) -> str | None:
    if not cam.source:
        return None
    # Allow either an RTSP URL (passed verbatim) or a path relative to
    # /data/video/.
    if "://" in cam.source:
        return cam.source
    p = VIDEO_ROOT / cam.source
    return str(p) if p.exists() else None


async def run_camera(cam: CameraCfg, store_id: str, bus: EventBus) -> None:
    """Main per-camera loop. Returns when the source is exhausted (and
    LOOP_VIDEO is False) or the task is cancelled."""
    # Lazy imports keep the synthetic image lean.
    import cv2  # type: ignore
    import supervision as sv  # type: ignore
    from ultralytics import YOLO  # type: ignore

    src = _resolve_source(cam)
    if src is None:
        log.warning("video.no_source", camera_id=cam.id, source=cam.source)
        return

    model = YOLO(MODEL_NAME)
    tracker = sv.ByteTrack()
    handler = make_handler(cam, store_id)

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        log.error("video.cap_open_failed", camera_id=cam.id, source=src)
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or float(cam.fps) or 25.0
    log.info("video.start", camera_id=cam.id, fps=fps, source=src)

    frame_idx = 0
    last_gc_frame = 0
    published = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if LOOP_VIDEO:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                break

            frame_idx += 1
            if frame_idx % FRAME_STRIDE != 0:
                continue

            ts = datetime.now(UTC)

            results: Any = model.predict(frame, classes=[0], conf=CONF_THRESHOLD, verbose=False)
            detections = sv.Detections.from_ultralytics(results[0])
            detections = tracker.update_with_detections(detections)

            if detections.tracker_id is not None:
                for xyxy, track_id in zip(detections.xyxy, detections.tracker_id, strict=False):
                    if track_id is None:
                        continue
                    fp = foot_point(tuple(xyxy))
                    for env in handler.update(int(track_id), fp, ts):
                        await bus.publish(env)
                        published += 1

            # Periodic garbage collection of stale tracks.
            if frame_idx - last_gc_frame > int(fps * 5):
                for env in handler.gc(ts):
                    await bus.publish(env)
                    published += 1
                last_gc_frame = frame_idx

            if frame_idx % int(fps * 30) == 0:
                log.info("video.progress", camera_id=cam.id, frames=frame_idx, published=published)

            # Yield to the event loop so other tasks (signals) run.
            await asyncio.sleep(0)
    finally:
        cap.release()
        # Final flush of any open zones.
        for env in handler.gc(datetime.now(UTC).replace(year=2999)):
            await bus.publish(env)


async def _main() -> None:
    _setup_logging()
    camera_id = os.environ.get("CAMERA_ID")
    if not camera_id:
        log.error("video.camera_id_unset")
        raise SystemExit(2)

    cfg: StoreCfg = load_store_cfg()
    cam = cfg.by_id(camera_id)
    bus = EventBus(REDIS_URL)
    for _ in range(15):
        try:
            await bus.ping()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)

    try:
        await run_camera(cam, cfg.store_id, bus)
    finally:
        await bus.close()


if __name__ == "__main__":
    asyncio.run(_main())
