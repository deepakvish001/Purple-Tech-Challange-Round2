"""Dump the FastAPI OpenAPI schema to docs/openapi.json.

Run from the repo root: `PYTHONPATH=. python scripts/dump_openapi.py`.
The output is committed so reviewers can browse the API surface without
running the service.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.api.main import app

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def main() -> None:
    schema = app.openapi()
    OUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(OUT.parents[1])}")


if __name__ == "__main__":
    main()
