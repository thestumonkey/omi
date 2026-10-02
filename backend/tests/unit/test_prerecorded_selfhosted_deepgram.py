"""Self-hosted: uploaded audio is transcribed by Deepgram from bytes. [fork-only]

Runs in a subprocess: the seam is chosen at import time from SELF_HOSTED.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = """
from unittest.mock import MagicMock, patch
import utils.stt.pre_recorded as pr
print('service', pr.get_prerecorded_service('multi')[0])
calls = []
fake_body = MagicMock(); fake_body.read.return_value = b'RIFFwav'
s3 = MagicMock(); s3.get_object.return_value = {'Body': fake_body}
with patch('utils.other.minio_storage._s3_client', return_value=s3), \\
     patch.object(pr, 'deepgram_prerecorded_from_bytes', lambda b, **kw: calls.append((b, kw['model'])) or []):
    pr.prerecorded('https://files.example.ts.net/omi-sync/syncing/u/a%20b.wav?X-Amz-Signature=x', language='multi')
print('bytes', calls[0][0].decode(), calls[0][1])
print('key', s3.get_object.call_args.kwargs['Bucket'], s3.get_object.call_args.kwargs['Key'])
"""


def _run(**env):
    full = {**os.environ, "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_dg", **env}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True)
    return out.stdout + out.stderr


def test_self_hosted_uploads_use_deepgram_bytes_from_internal_storage():
    out = _run(SELF_HOSTED="true", DEEPGRAM_API_KEY="k", S3_PRESIGN_ENDPOINT_URL="https://files.example.ts.net")
    assert "service deepgram" in out
    assert "bytes RIFFwav nova-3" in out
    assert "key omi-sync syncing/u/a b.wav" in out


def test_upstream_default_is_unchanged():
    out = _run(SELF_HOSTED="", DEEPGRAM_API_KEY="k")
    assert "service deepgram" not in out
