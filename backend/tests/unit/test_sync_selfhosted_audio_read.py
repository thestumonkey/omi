"""Self-hosted: sync reads its own signed audio URLs from storage. [fork-only]

Signed URLs carry MinIO's tailnet name, which pods cannot resolve, so the
network download failed and synced recordings lost speaker matching and audio.

Runs in a subprocess: the seam is chosen at import time from SELF_HOSTED, and
reloading utils.sync.pipeline in-process would break other tests' references.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = """
from unittest.mock import MagicMock, patch
import utils.sync.pipeline as p
body = MagicMock(); body.read.return_value = b'RIFFwav'
s3 = MagicMock(); s3.get_object.return_value = {'Body': body}
fetched = []
with patch('utils.other.minio_storage._s3_client', return_value=s3), \\
     patch.object(p.httpx, 'get', lambda url, **kw: fetched.append(url) or (_ for _ in ()).throw(OSError('no dns'))):
    print('own', p._download_audio_bytes('https://files.example.ts.net/sync-temp/u/a%20b.wav?X-Amz-Signature=x'))
    call = s3.get_object.call_args
    print('key', call and call.kwargs['Bucket'], call and call.kwargs['Key'])
    print('other', p._download_audio_bytes('https://evil.example/x/y'), len(fetched))
    s3.get_object.side_effect = RuntimeError('gone')
    print('missing', p._download_audio_bytes('http://minio:9000/sync-temp/u/c.wav'))
"""


def _run(**env):
    full = {
        **os.environ,
        "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_audio_read",
        "S3_PRESIGN_ENDPOINT_URL": "https://files.example.ts.net",
        "S3_ENDPOINT_URL": "http://minio:9000",
        **env,
    }
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=full, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_self_hosted_reads_own_urls_from_storage():
    out = _run(SELF_HOSTED="true")
    assert "own b'RIFFwav'" in out
    assert "key sync-temp u/a b.wav" in out


def test_self_hosted_other_urls_keep_the_network_download():
    assert "other None 1" in _run(SELF_HOSTED="true")


def test_self_hosted_storage_failure_returns_none():
    assert "missing None" in _run(SELF_HOSTED="true")


def test_upstream_downloads_over_the_network():
    out = _run(SELF_HOSTED="")
    assert "own None" in out
