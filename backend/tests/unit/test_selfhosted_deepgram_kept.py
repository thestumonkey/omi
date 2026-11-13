"""Self-hosted: Deepgram still serves live and uploaded audio. [fork-only]

Upstream retired Deepgram (e2439263aa): it removed Deepgram from the provider
policy and made the pre-recorded client refuse every call. A self-hosted server
uses its own Deepgram key and runs no other speech-to-text service, so without
the fork blocks in config/stt_provider_policy.py and utils/stt/pre_recorded.py
no recording is transcribed.

Runs in a subprocess: the blocks are chosen at import time from the environment.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = """
from utils.stt.streaming import get_stt_service_for_language
from utils.stt import pre_recorded
service, _, model = get_stt_service_for_language('en')
print('live', getattr(service, 'value', service), model)
print('upload', type(pre_recorded._deepgram_client_for_request()).__name__)
"""


def _probe(extra):
    env = {
        **{k: v for k, v in os.environ.items() if k not in ('SELF_HOSTED', 'DEEPGRAM_API_KEY')},
        "PYTHONPATH": str(BACKEND),
        "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_deepgram_kept",
        "STT_SERVICE_MODELS": "dg-nova-3",
        **extra,
    }
    return subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=env, capture_output=True, text=True)


def test_self_hosted_with_a_deepgram_key_uses_deepgram():
    r = _probe({"SELF_HOSTED": "true", "DEEPGRAM_API_KEY": "test-key"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert "live deepgram nova-3" in r.stdout
    assert "upload DeepgramClient" in r.stdout


def test_without_self_hosted_upstream_retirement_holds():
    r = _probe({"DEEPGRAM_API_KEY": "test-key"})
    assert "live deepgram" not in r.stdout
    assert "upload DeepgramClient" not in r.stdout
