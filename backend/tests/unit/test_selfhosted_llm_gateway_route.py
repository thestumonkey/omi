"""Self-hosted: managed LLM calls use the self-hosted server, not the Omi gateway. [fork-only]

Upstream sends all company-paid generation through the Omi LLM gateway service.
A self-hosted cluster does not run it, so with SELF_HOSTED_LLM_URL set the
gateway chat clients must come from utils/llm/selfhosted.py.

Runs in a subprocess: the seam is chosen at import time from the environment.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = """
from utils.llm.clients import get_llm, get_llm_gateway_chat_structured
for feature in ('memory_l2', 'conv_structure', 'chat_responses'):
    llm = get_llm(feature)
    print('llm', feature, type(llm).__name__, llm.openai_api_base)
print('structured', type(get_llm_gateway_chat_structured()).__name__)
"""


def test_managed_llm_calls_use_the_self_hosted_server():
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "ENCRYPTION_SECRET": "omi_test_secret_for_selfhosted_gateway_route",
        "SELF_HOSTED_LLM_URL": "http://llm.example:8000/v1",
        "SELF_HOSTED_LLM_MODEL": "local-model",
    }
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=BACKEND, env=env, capture_output=True, text=True, check=True
    ).stdout
    for feature in ("memory_l2", "conv_structure", "chat_responses"):
        assert f"llm {feature} SelfHostedChatOpenAI http://llm.example:8000/v1" in out
    assert "structured SelfHostedChatOpenAI" in out
