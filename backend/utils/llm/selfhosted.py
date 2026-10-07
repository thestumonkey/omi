"""Self-hosted LLM client. [fork-only]

When SELF_HOSTED_LLM_URL is set, every default (non-BYOK, non-gateway) chat
client routes to one OpenAI-compatible endpoint (Ollama, llama.cpp, lemonade)
instead of OpenAI / OpenRouter / Gemini. Wired in by the tail block of
utils/llm/providers.py, which rebinds ``get_default_client``.

Settings:
  SELF_HOSTED_LLM_URL              OpenAI-compatible base URL, including /v1
  SELF_HOSTED_LLM_MODEL            replaces every model in the QoS profile
                                   (the cloud names do not exist locally);
                                   the model picked in the app's settings
                                   (``current_model``) wins over it
  SELF_HOSTED_LLM_KEY              sent as the API key (default "sk-local")
  SELF_HOSTED_LLM_MAX_CONCURRENCY  max in-flight requests (default 1; 0 = no limit)
  SELF_HOSTED_LLM_READ_TIMEOUT     seconds to wait for a reply (default 300);
                                   background jobs with long prompts need more
  SELF_HOSTED_LLM_MAX_TOKENS       reply-length cap sent when the caller sets none
                                   (default 8192; servers default lower, e.g.
                                   lemonade 4096, which cuts long JSON replies)
  SELF_HOSTED_LLM_THINKING         "on" lets hybrid reasoning models (Qwen3, ...)
                                   think before answering; default off, because
                                   the backend's calls want short, direct JSON

Embeddings are not affected.
"""

import json
import logging
import os
import re
from contextvars import ContextVar
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
_SELF_HOSTED_LLM_MAX_TOKENS = int(os.environ.get('SELF_HOSTED_LLM_MAX_TOKENS', '8192') or '8192')
_SELF_HOSTED_LLM_READ_TIMEOUT = float(os.environ.get('SELF_HOSTED_LLM_READ_TIMEOUT', '300') or '300')

# Text that PydanticOutputParser.get_format_instructions() puts in a prompt.
_JSON_FORMAT_MARKERS = ('formatted as a JSON instance',)
_FENCED_BLOCK = re.compile(r'^```[a-zA-Z]*\s*\n?(.*?)\n?\s*```$', re.DOTALL)
# Hybrid reasoning models (Qwen3 and similar) think unless their chat template
# is told not to; llama.cpp passes these kwargs to the template, and templates
# without the variable ignore it.
_THINKING = os.environ.get('SELF_HOSTED_LLM_THINKING', '').strip().lower() in ('1', 'true', 'yes', 'on')
_TEMPLATE_KWARGS = {} if _THINKING else {'enable_thinking': False}

# PydanticOutputParser embeds the JSON schema after this phrase.
_OUTPUT_SCHEMA = re.compile(r'Here is the output schema:\s*```\s*(\{.*?\})\s*```', re.DOTALL)
# An unquoted non-ASCII value, e.g. `"emoji": 📝,` — the commonest local-model slip.
_BARE_NON_ASCII_VALUE = re.compile(r'(:\s*)([^\x00-\x7f\s][^,\n}\]]*?)(\s*[,}\]\n])')

# Set while building a request that asked for JSON; read when its reply comes back.
# `None` = not a JSON request. Otherwise the single array property of the schema, or "".
_json_reply_wrap_key: ContextVar[Optional[str]] = ContextVar('selfhosted_json_reply_wrap_key', default=None)


def _message_texts(messages: Any):
    for message in messages or []:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            content = ' '.join(part.get('text', '') for part in content if isinstance(part, dict))
        if isinstance(content, str):
            yield content


def _asks_for_json(messages: Any) -> bool:
    return any(marker in text for text in _message_texts(messages) for marker in _JSON_FORMAT_MARKERS)


def _single_array_property(messages: Any) -> str:
    """Name of the schema's only property when that property is a list, else "".

    Small models asked for `{"action_items": [...]}` often answer with just the
    list. Only a one-property schema makes the wrapper unambiguous.
    """
    for text in _message_texts(messages):
        match = _OUTPUT_SCHEMA.search(text)
        if not match:
            continue
        try:
            properties = json.loads(match.group(1)).get('properties') or {}
        except (ValueError, AttributeError):
            return ''
        if len(properties) == 1:
            ((name, spec),) = properties.items()
            if isinstance(spec, dict) and spec.get('type') == 'array':
                return name
        return ''
    return ''


def _repair_json_reply(text: str, wrap_key: str) -> str:
    """Best-effort fix of a JSON reply the strict parser would reject.

    Returns `text` unchanged when it is already valid (and needs no wrapper) or
    when no repair produces valid JSON, so the parser still reports the real error.
    """
    candidates = [text, _BARE_NON_ASCII_VALUE.sub(r'\1"\2"\3', text)]
    start, end = text.find('{'), text.rfind('}')
    if 0 < start < end:  # prose around the object
        inner = text[start : end + 1]
        candidates += [inner, _BARE_NON_ASCII_VALUE.sub(r'\1"\2"\3', inner)]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if wrap_key and isinstance(parsed, list):
            return json.dumps({wrap_key: parsed})
        return candidate
    return text


class SelfHostedChatOpenAI(ChatOpenAI):
    """ChatOpenAI that makes weak local models produce parseable JSON.

    Upstream asks for structured output by appending parser format
    instructions to the prompt. Cloud models comply; small local models wrap
    the JSON in prose or code fences, the strict parser fails, and the user sees
    empty titles and summaries. When a request carries those instructions (and
    no tools or explicit response format), this turns on the server's JSON mode,
    which grammar-constrains generation to valid JSON where the server supports
    it. Not every server does (lemonade still returns `"emoji": 📝`), so replies
    to those requests are also repaired when they fail to parse. Any reply that
    is just a fenced code block is unwrapped. Upstream call sites stay unchanged.
    """

    def _get_request_payload(self, input_: Any, *, stop: Optional[list] = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if _SELF_HOSTED_LLM_MAX_TOKENS > 0 and not (payload.get('max_tokens') or payload.get('max_completion_tokens')):
            payload['max_tokens'] = _SELF_HOSTED_LLM_MAX_TOKENS
        if _TEMPLATE_KWARGS:
            extra = dict(payload.get('extra_body') or {})
            extra.setdefault('chat_template_kwargs', dict(_TEMPLATE_KWARGS))
            payload['extra_body'] = extra
        messages = payload.get('messages')
        if 'tools' not in payload and 'response_format' not in payload and _asks_for_json(messages):
            payload['response_format'] = {'type': 'json_object'}
            _json_reply_wrap_key.set(_single_array_property(messages))
        else:
            _json_reply_wrap_key.set(None)
        return payload

    def _create_chat_result(self, response: Any, generation_info: Optional[Dict] = None) -> Any:
        result = super()._create_chat_result(response, generation_info)
        for generation in result.generations:
            content = generation.message.content
            if isinstance(content, str):
                match = _FENCED_BLOCK.match(content.strip())
                if match:
                    content = match.group(1).strip()
                wrap_key = _json_reply_wrap_key.get()
                if wrap_key is not None:
                    content = _repair_json_reply(content, wrap_key)
                if content != generation.message.content:
                    generation.message.content = content
                    generation.text = content
        return result


_http_client: Optional[httpx.Client] = None
_llm_cache: Dict[tuple, Any] = {}

# ── Chat model picked in the app's settings ──────────────────────────────────
#
# One server-wide choice (utils/selfhosted_config, group "llm"), so omi-backend,
# the pusher and the desktop backend all use it within CACHE_SECONDS.


def current_model() -> str:
    """Chat model to use now: the one saved from settings, else SELF_HOSTED_LLM_MODEL."""
    from utils import selfhosted_config

    model = selfhosted_config.read('llm').get('model')
    return model.strip() if isinstance(model, str) and model.strip() else _SELF_HOSTED_LLM_MODEL


def save_model(model: Optional[str]) -> None:
    """Store the settings choice; None or "" goes back to SELF_HOSTED_LLM_MODEL."""
    from utils import selfhosted_config

    selfhosted_config.write('llm', {'model': (model or '').strip() or None})


def default_model() -> str:
    return _SELF_HOSTED_LLM_MODEL


def list_models() -> list[str]:
    """Model ids the self-hosted server offers (OpenAI `GET /models`)."""
    response = httpx.get(
        _join(SELF_HOSTED_LLM_URL, 'models'),
        headers={'authorization': f'Bearer {_SELF_HOSTED_LLM_KEY}'},
        timeout=10.0,
    )
    response.raise_for_status()
    return sorted({item['id'] for item in response.json().get('data', []) if isinstance(item, dict) and item.get('id')})


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
            timeout=httpx.Timeout(connect=10.0, read=_SELF_HOSTED_LLM_READ_TIMEOUT, write=30.0, pool=None),
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
    model = current_model()
    key = (streaming, temperature, model)
    if key not in _llm_cache:
        kwargs: Dict[str, Any] = {
            'api_key': _SELF_HOSTED_LLM_KEY,
            'base_url': SELF_HOSTED_LLM_URL,
            'callbacks': [get_usage_callback()],
            'request_timeout': _SELF_HOSTED_LLM_READ_TIMEOUT,
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
        _llm_cache[key] = SelfHostedChatOpenAI(model=model, **kwargs)
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


def make_embeddings():
    """Embeddings client for the self-hosted embedding server (OpenAI-compatible).

    Replaces upstream's OpenAI text-embedding-3-large client (tail block of
    utils/llm/clients.py) when SELF_HOSTED_EMBED_URL is set. Inputs go as plain
    strings: the token-chunking that langchain does for OpenAI models uses
    OpenAI's tokenizer, which other servers do not accept.
    """
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=_SELF_HOSTED_EMBED_MODEL,
        base_url=_SELF_HOSTED_EMBED_URL,
        api_key=_SELF_HOSTED_LLM_KEY,
        check_embedding_ctx_length=False,
        request_timeout=120,
        max_retries=2,
    )


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
    payload['model'] = current_model()
    if _SELF_HOSTED_LLM_MAX_TOKENS > 0 and not (payload.get('max_tokens') or payload.get('max_completion_tokens')):
        payload['max_tokens'] = _SELF_HOSTED_LLM_MAX_TOKENS
    if _TEMPLATE_KWARGS:
        payload['chat_template_kwargs'] = dict(_TEMPLATE_KWARGS)
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
        timeout=httpx.Timeout(connect=10.0, read=_SELF_HOSTED_LLM_READ_TIMEOUT, write=30.0, pool=None),
    )
