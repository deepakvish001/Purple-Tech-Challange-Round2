"""Load camera + zone YAML configs into typed dataclasses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

CamRole = Literal[
    "foh_top_shelves",
    "foh_bottom_shelves",
    "entry_exit",
    "back_of_house",
    "cash_counter",
]

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))


@dataclass(frozen=True)
class Tripwire:
    line_id: str
    segment: tuple[tuple[int, int], tuple[int, int]]
    inside_side: Literal["left", "right", "top", "bottom"] = "left"
    debounce_frames: int = 5


@dataclass(frozen=True)
class Zone:
    id: str
    label: str
    polygon: list[tuple[int, int]]


@dataclass(frozen=True)
class CameraCfg:
    id: str
    role: CamRole
    source: str | None
    codec: str | None = None
    fps: int = 25
    tripwire: Tripwire | None = None
    zones: list[Zone] = field(default_factory=list)


@dataclass(frozen=True)
class StoreCfg:
    store_id: str
    cameras: list[CameraCfg]

    def by_id(self, camera_id: str) -> CameraCfg:
        for c in self.cameras:
            if c.id == camera_id:
                return c
        raise KeyError(f"camera_id={camera_id!r} not in cameras.yaml")


def _load_yaml(path: Path) -> dict:
    with path.open("r") as f:
        return yaml.safe_load(f)


def _load_zones(zones_file: str | None) -> list[Zone]:
    if not zones_file:
        return []
    data = _load_yaml(CONFIG_DIR / zones_file)
    return [
        Zone(id=z["id"], label=z["label"], polygon=[tuple(p) for p in z["polygon"]])
        for z in data.get("zones", [])
    ]


def _load_tripwire(raw: dict | None) -> Tripwire | None:
    if not raw:
        return None
    seg = raw["segment"]
    return Tripwire(
        line_id=raw["line_id"],
        segment=((seg[0][0], seg[0][1]), (seg[1][0], seg[1][1])),
        inside_side=raw.get("inside_side", "left"),
        debounce_frames=int(raw.get("debounce_frames", 5)),
    )


def load_store_cfg(path: Path | None = None) -> StoreCfg:
    """Load `config/cameras.yaml` + referenced per-camera zone files."""
    path = path or (CONFIG_DIR / "cameras.yaml")
    raw = _load_yaml(path)
    cams: list[CameraCfg] = []
    for c in raw["cameras"]:
        cams.append(
            CameraCfg(
                id=c["id"],
                role=c["role"],
                source=c.get("source"),
                codec=c.get("codec"),
                fps=int(c.get("fps", 25)),
                tripwire=_load_tripwire(c.get("tripwire")),
                zones=_load_zones(c.get("zones_file")),
            )
        )
    return StoreCfg(store_id=raw["store_id"], cameras=cams)
