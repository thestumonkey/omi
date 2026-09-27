"""Self-hosted LLM client. [fork-only]

When SELF_HOSTED_LLM_URL is set, every default (non-BYOK, non-gateway) chat
client routes to one OpenAI-compatible endpoint (Ollama, llama.cpp, lemonade)
instead of OpenAI / OpenRouter / Gemini. Wired in by the tail block of
utils/llm/providers.py, which rebinds ``get_default_client``.

Settings:
  SELF_HOSTED_LLM_URL              OpenAI-compatible base URL, including /v1
  SELF_HOSTED_LLM_MODEL            replaces every model in the QoS profile
                                   (the cloud names do not exist locally)
  SELF_HOSTED_LLM_KEY              sent as the API key (default "sk-local")
  SELF_HOSTED_LLM_MAX_CONCURRENCY  max in-flight requests (default 1; 0 = no limit)

Embeddings are not affected.
"""

import logging
import os
import re
from typing import Any, Dict, Optional

import httpx
from langchain_openai import ChatOpenAI

from utils.llm.usage_tracker import get_usage_callback

logger = logging.getLogger(__name__)

SELF_HOSTED_LLM_URL = os.environ.get('SELF_HOSTED_LLM_URL', '').strip()
_SELF_HOSTED_LLM_MODEL = os.environ.get('SELF_HOSTED_LLM_MODEL', '').strip() or 'gpt-4.1-mini'
_SELF_HOSTED_LLM_KEY = os.environ.get('SELF_HOSTED_LLM_KEY', '').strip() or 'sk-local'

# A llama.cpp/lemonade server typically runs `--parallel 1`: one lane shared by
# every loaded model. Post-processing fans out several extractions per finished
# conversation onto a large worker pool; without a limit they pile into the
# server's queue, holding connections until callers time out.
_SELF_HOSTED_LLM_MAX_CONCURRENCY = int(os.environ.get('SELF_HOSTED_LLM_MAX_CONCURRENCY', '1') or '1')

# Text that PydanticOutputParser.get_format_instructions() puts in a prompt.
_JSON_FORMAT_MARKERS = ('formatted as a JSON instance',)
_FENCED_BLOCK = re.compile(r'^```[a-zA-Z]*\s*\n?(.*?)\n?\s*```$', re.DOTALL)


def _asks_for_json(messages: Any) -> bool:
    for message in messages or []:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            content = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
        if isinstance(content, str) and any(marker in content for marker in _JSON_FORMAT_MARKERS):
            return True
    return False


class SelfHostedChatOpenAI(ChatOpenAI):
    """ChatOpenAI that makes weak local models produce parseable JSON.

    Upstream asks for structured output by appending parser format
    instructions to the prompt. Cloud models comply; small local models wrap
    the JSON in prose or code fences, the strict parser fails, and the user sees
    empty titles and summaries. When a request carries those instructions (and
    no tools or explicit response format), this turns on the server's JSON mode,
    which grammar-constrains generation to valid JSON. Any reply that is just a
    fenced code block is also unwrapped. Upstream call sites stay unchanged.
    """

    def _get_request_payload(self, input_: Any, *, stop: Optional[list] = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if 'tools' not in payload and 'response_format' not in payload and _asks_for_json(payload.get('messages')):
            payload['response_format'] = {'type': 'json_object'}
        return payload

    def _create_chat_result(self, response: Any, generation_info: Optional[Dict] = None) -> Any:
        result = super()._create_chat_result(response, generation_info)
        for generation in result.generations:
            content = generation.message.content
            if isinstance(content, str):
                match = _FENCED_BLOCK.match(content.strip())
                if match:
                    generation.message.content = match.group(1).strip()
                    generation.text = generation.message.content
        return result


_http_client: Optional[httpx.Client] = None
_llm_cache: Dict[tuple, Any] = {}


def _get_selfhosted_http_client() -> Optional[httpx.Client]:
    """Shared httpx client that admits only _SELF_HOSTED_LLM_MAX_CONCURRENCY requests.

    The connection-pool size is the concurrency limit: httpx makes a caller wait
    for a free connection, so surplus requests wait here, holding no socket,
    instead of queueing inside the LLM server. ``pool=None`` waits forever rather
    than failing, because post-processing does not retry: a dropped memory
    extraction loses those memories for good.

    Only the sync client is limited. The async callers are low-volume, and one
    httpx.AsyncClient shared across this backend's several event loops risks
    binding pooled connections to a dead loop.
    """
    global _http_client
    if _SELF_HOSTED_LLM_MAX_CONCURRENCY <= 0:
        return None
    if _http_client is None:
        _http_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=_SELF_HOSTED_LLM_MAX_CONCURRENCY,
                max_keepalive_connections=_SELF_HOSTED_LLM_MAX_CONCURRENCY,
            ),
            # Set explicitly: a caller-supplied client bypasses langchain's
            # request_timeout, and httpx's 5s default would fail any request
            # waiting for the single connection.
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=None),
        )
        logger.info('self-hosted LLM: limiting to %d concurrent request(s)', _SELF_HOSTED_LLM_MAX_CONCURRENCY)
    return _http_client


def get_selfhosted_llm(streaming: bool = False, options: Optional[Dict[str, Any]] = None) -> SelfHostedChatOpenAI:
    """The one client every default route collapses onto.

    Generous timeout and retries tolerate on-demand model loading: a cold start
    on lemonade can take minutes. Feature options such as temperature and
    extra_body pass through; the cloud model name does not.
    """
    options = options or {}
    temperature = options.get('temperature')
    key = (streaming, temperature)
    if key not in _llm_cache:
        kwargs: Dict[str, Any] = {
            'api_key': _SELF_HOSTED_LLM_KEY,
            'base_url': SELF_HOSTED_LLM_URL,
            'callbacks': [get_usage_callback()],
            'request_timeout': 300,
            'max_retries': 2,
        }
        http_client = _get_selfhosted_http_client()
        if http_client is not None:
            kwargs['http_client'] = http_client
        if temperature is not None:
            kwargs['temperature'] = temperature
        if streaming:
            kwargs['streaming'] = True
            kwargs['stream_options'] = {'include_usage': True}
        _llm_cache[key] = SelfHostedChatOpenAI(model=_SELF_HOSTED_LLM_MODEL, **kwargs)
    return _llm_cache[key]
