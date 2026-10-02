"""Self-hosted: backfill uploads take the fresh lane without Cloud Tasks. [fork-only]

Runs in a subprocess: the seam is chosen at import time from SELF_HOSTED, and
reloading routers.sync in-process would break other tests' references.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = (
    "from routers.sync import classify_sync_lane\n"
    "d = classify_sync_lane(['audio_abc_opus_fs320_16000_1_1780000000.bin'], client_device_id=None, now=1780000060)\n"
    "print(d.lane.value, d.reason)\n"
)


def _lane(**env):
    full = {**os.environ, "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_backfill", **env}
    full.pop("SYNC_DISPATCH_MODE", None) if "SYNC_DISPATCH_MODE" not in env else None
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True, check=True
    )
    return out.stdout.split()[-2:]


def test_self_hosted_unbound_recording_takes_fresh_lane():
    assert _lane(SELF_HOSTED="true") == ["fresh", "unbound_capture_time"]


def test_self_hosted_with_cloud_tasks_keeps_backfill():
    assert _lane(SELF_HOSTED="true", SYNC_DISPATCH_MODE="cloud_tasks")[0] == "backfill"


def test_upstream_default_keeps_backfill():
    assert _lane(SELF_HOSTED="")[0] == "backfill"
