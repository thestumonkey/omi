"""Self-hosted: audio-merge jobs run in-process (AUDIO_MERGE_DISPATCH_MODE=local). [fork-only]

Runs in a subprocess: the seam is chosen at import time.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = """
import time, threading
from unittest.mock import patch
from fastapi.responses import JSONResponse
import utils.cloud_tasks as ct
from utils.sync import playback
print('enabled', ct.is_audio_merge_dispatch_enabled(), playback.is_audio_merge_dispatch_enabled())
calls = []
gate = threading.Event()
async def fake_handler(request, task_retry_count=0):
    calls.append(((await request.json())['conversation_id'], task_retry_count))
    gate.wait(5)
    return JSONResponse(status_code=200, content={})
with patch('routers.sync.run_audio_merge_job', fake_handler):
    payload = {'schema_version': 2, 'uid': 'u', 'conversation_id': 'c1', 'fingerprint': 'f'}
    ct.enqueue_audio_merge_job(payload)
    ct.enqueue_audio_merge_job(payload)  # deduped while in flight
    time.sleep(0.5); gate.set(); time.sleep(0.5)
print('calls', calls)
"""


def _run(**env):
    full = {**os.environ, "ENCRYPTION_SECRET": "omi_test_secret_for_local_audio_merge_unit_test", **env}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True)
    return out.stdout + out.stderr


def test_local_mode_runs_handler_once_per_task():
    out = _run(SELF_HOSTED="true", AUDIO_MERGE_DISPATCH_MODE="local")
    assert "enabled True True" in out
    assert "calls [('c1', 0)]" in out


def test_upstream_default_unchanged():
    out = _run(SELF_HOSTED="true", AUDIO_MERGE_DISPATCH_MODE="inline")
    assert "enabled False False" in out
