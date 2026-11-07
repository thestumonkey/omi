"""Self-hosted: a file with a bad timestamp is left out, not the whole batch. [fork-only]

Runs in a subprocess: the seam is chosen at import time from SELF_HOSTED, and
reloading routers.sync in-process would break other tests' references.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = (
    "import io, os, tempfile\n"
    "os.chdir(tempfile.mkdtemp())\n"
    "from starlette.datastructures import UploadFile\n"
    "from fastapi import HTTPException\n"
    "import routers.sync as s\n"
    "def f(name): return UploadFile(file=io.BytesIO(b'x'), filename=name)\n"
    "good = 'audio_abc_opus_fs320_16000_1_1780000000.bin'\n"
    "bad = 'audio_abc_opus_fs320_16000_1_4102444800.bin'  # year 2100\n"
    "try:\n"
    "    paths = s._retrieve_file_paths_v2([f(good), f(bad)], 'uid', 'job')\n"
    "    print('mixed', len(paths))\n"
    "except HTTPException as e:\n"
    "    print('mixed', e.status_code)\n"
    "try:\n"
    "    s._retrieve_file_paths_v2([f(bad)], 'uid', 'job2')\n"
    "    print('allbad', 'ok')\n"
    "except HTTPException as e:\n"
    "    print('allbad', e.status_code)\n"
)


def _run(**env):
    full = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_bad_timestamp",
        **env,
    }
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True, check=True
    )
    return dict(line.split() for line in out.stdout.splitlines() if line.startswith(("mixed", "allbad")))


def test_self_hosted_skips_only_the_bad_file():
    assert _run(SELF_HOSTED="true") == {"mixed": "1", "allbad": "400"}


def test_upstream_default_refuses_the_whole_batch():
    assert _run(SELF_HOSTED="") == {"mixed": "400", "allbad": "400"}
