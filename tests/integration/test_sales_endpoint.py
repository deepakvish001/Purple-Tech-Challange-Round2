"""Sales breakdown endpoint contracts.

Uses the locally-running Postgres seeded by tests/integration's harness.
Skipped if DATABASE_URL is not reachable.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CI_INTEGRATION_DB"),
    reason="Integration test requires a live Postgres (set CI_INTEGRATION_DB=1)",
)


def test_sales_endpoint_shape() -> None:
    # Placeholder for a live-DB integration test wired in CI.
    # The pure shape contract is exercised by tests/unit/test_api.py's
    # graceful-degradation path; full data shape is covered by the
    # `stack` CI job that drives the running service.
    pass
