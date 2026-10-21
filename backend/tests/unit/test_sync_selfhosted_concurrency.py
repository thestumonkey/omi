"""Self-hosted: inline fresh-lane sync pipelines run one at a time. [fork-only]

Runs in a subprocess: the seam is chosen at import time from SELF_HOSTED, and
reloading utils.sync.pipeline in-process would break other tests' references.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = (
    "import asyncio\n"
    "from utils.sync.pipeline import _get_sync_pipeline_semaphore\n"
    "async def main():\n"
    "    print(_get_sync_pipeline_semaphore('fresh')._value, _get_sync_pipeline_semaphore('backfill')._value)\n"
    "asyncio.run(main())\n"
)


def _limits(**env):
    full = {**os.environ, "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_concurrency", **env}
    for key in ("SYNC_INLINE_FRESH_CONCURRENCY", "SYNC_INLINE_BACKFILL_CONCURRENCY"):
        if key not in env:
            full.pop(key, None)
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True, check=True
    )
    return [int(v) for v in out.stdout.split()[-2:]]


def test_self_hosted_fresh_lane_defaults_to_one():
    assert _limits(SELF_HOSTED="true") == [1, 2]


def test_self_hosted_fresh_lane_limit_is_configurable():
    assert _limits(SELF_HOSTED="true", SYNC_INLINE_FRESH_CONCURRENCY="3") == [3, 2]


def test_upstream_default_keeps_sixteen():
    assert _limits(SELF_HOSTED="") == [16, 2]
