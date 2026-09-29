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

import json
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


# ── Omi LLM gateway stand-in (desktop backend) ───────────────────────────────
#
# The desktop backend's managed LLM traffic (Gemini proxy, desktop chat,
# proactivity) already speaks OpenAI chat to the internal "LLM gateway", after
# upstream's own Gemini<->OpenAI translation. Pointing that gateway client at the
# self-hosted server needs only a request rewrite: the gateway's lane ids
# ("omi:auto:...") become the local model name, gateway-only fields are dropped,
# and embeddings go to the embedding server. Wired in by the tail block of
# utils/http_client.py (get_llm_gateway_client) when SELF_HOSTED_LLM_URL is set.
#
#   SELF_HOSTED_EMBED_URL    OpenAI-compatible embeddings base, including /v1
#                            (default: SELF_HOSTED_LLM_URL)
#   SELF_HOSTED_EMBED_MODEL  embedding model name (default nomic-embed-text)

_SELF_HOSTED_EMBED_URL = os.environ.get('SELF_HOSTED_EMBED_URL', '').strip()
_SELF_HOSTED_EMBED_MODEL = os.environ.get('SELF_HOSTED_EMBED_MODEL', '').strip() or 'nomic-embed-text'

# Keys the OpenAI chat/embeddings API defines; anything else the gateway
# understood (google, metadata, prompt_cache_options, ...) is dropped.
_OPENAI_CHAT_KEYS = frozenset(
    {
        'messages',
        'model',
        'stream',
        'stream_options',
        'temperature',
        'top_p',
        'max_tokens',
        'max_completion_tokens',
        'stop',
        'tools',
        'tool_choice',
        'parallel_tool_calls',
        'response_format',
        'n',
        'presence_penalty',
        'frequency_penalty',
        'seed',
        'logit_bias',
        'user',
    }
)
_OPENAI_EMBED_KEYS = frozenset({'input', 'model', 'dimensions', 'encoding_format', 'user'})


def _join(base: str, suffix: str) -> str:
    return base.rstrip('/') + '/' + suffix.lstrip('/')


def rewrite_gateway_request(path: str, body: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Map one gateway request (path under /v1, JSON body) onto the self-hosted servers.

    Returns the absolute target URL and the body to send.
    """
    if path.endswith('/embeddings'):
        payload = {k: v for k, v in body.items() if k in _OPENAI_EMBED_KEYS}
        payload['model'] = _SELF_HOSTED_EMBED_MODEL
        # Local embedding models have a fixed width; the OpenAI `dimensions`
        # knob is not honoured by them and some servers reject it.
        payload.pop('dimensions', None)
        return _join(_SELF_HOSTED_EMBED_URL or SELF_HOSTED_LLM_URL, 'embeddings'), payload
    payload = {k: v for k, v in body.items() if k in _OPENAI_CHAT_KEYS}
    payload['model'] = _SELF_HOSTED_LLM_MODEL
    return _join(SELF_HOSTED_LLM_URL, path.split('/v1', 1)[-1] or 'chat/completions'), payload


class _SelfHostedGatewayTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == 'POST' and ('/chat/completions' in path or '/embeddings' in path):
            body = json.loads((await request.aread()) or b'{}')
            target, payload = rewrite_gateway_request(path, body)
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ('host', 'content-length', 'authorization') and not k.lower().startswith('x-omi')
            }
            headers['authorization'] = f'Bearer {_SELF_HOSTED_LLM_KEY}'
            request = httpx.Request(
                'POST', target, headers=headers, content=json.dumps(payload).encode(), extensions=request.extensions
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_gateway_client() -> httpx.AsyncClient:
    """AsyncClient that serves gateway calls from the self-hosted servers.

    Timeouts are set here because callers pass none (the gateway client's own
    are 20s, too short for a local model). ``pool=None`` queues requests beyond
    the concurrency cap instead of failing them.
    """
    limit = _SELF_HOSTED_LLM_MAX_CONCURRENCY if _SELF_HOSTED_LLM_MAX_CONCURRENCY > 0 else 24
    inner = httpx.AsyncHTTPTransport(
        limits=httpx.Limits(max_connections=limit, max_keepalive_connections=limit),
    )
    return httpx.AsyncClient(
        transport=_SelfHostedGatewayTransport(inner),
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=None),
    )
