"""SelfHostedChatOpenAI turns on JSON mode only for prompts that ask for JSON. [fork-only]"""

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel

from utils.llm.selfhosted import SelfHostedChatOpenAI


class _Title(BaseModel):
    title: str


def _llm():
    return SelfHostedChatOpenAI(model="llama", api_key="sk-local", base_url="http://localhost:9/v1")


def _response(content):
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "llama",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


def test_prompt_with_parser_instructions_gets_json_mode():
    instructions = PydanticOutputParser(pydantic_object=_Title).get_format_instructions()
    payload = _llm()._get_request_payload([SystemMessage(instructions), HumanMessage("name this")])
    assert payload["response_format"] == {"type": "json_object"}


def test_plain_chat_prompt_is_left_alone():
    payload = _llm()._get_request_payload([HumanMessage("how was my day?")])
    assert "response_format" not in payload


def test_tool_calls_are_left_alone():
    instructions = PydanticOutputParser(pydantic_object=_Title).get_format_instructions()
    llm = _llm().bind_tools([_Title])
    payload = llm.bound._get_request_payload([HumanMessage(instructions)], **llm.kwargs)
    assert "response_format" not in payload


def test_fenced_json_reply_is_unwrapped():
    result = _llm()._create_chat_result(_response('```json\n{"title": "Standup"}\n```'))
    assert result.generations[0].message.content == '{"title": "Standup"}'


def test_ordinary_reply_with_code_is_unchanged():
    text = "Here you go:\n```python\nprint(1)\n```"
    result = _llm()._create_chat_result(_response(text))
    assert result.generations[0].message.content == text


# ── LLM gateway stand-in (desktop backend) ───────────────────────────────────


def _selfhosted(monkeypatch, **env):
    import importlib

    import utils.llm.selfhosted as module

    for key, value in {
        "SELF_HOSTED_LLM_URL": "http://llm.local/v1",
        "SELF_HOSTED_LLM_MODEL": "llama",
        "SELF_HOSTED_EMBED_URL": "http://embed.local:11434/v1",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(module)


def test_gateway_chat_gets_local_model_and_loses_gateway_fields(monkeypatch):
    module = _selfhosted(monkeypatch)
    url, body = module.rewrite_gateway_request(
        "/v1/chat/completions",
        {
            "model": "omi:auto:chat-agent",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "tools": [{"type": "function"}],
            "metadata": {"feature": "x"},
            "prompt_cache_options": {"mode": "explicit"},
            "google": {"thinking_config": {}},
        },
    )
    assert url == "http://llm.local/v1/chat/completions"
    assert body == {
        "model": "llama",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "tools": [{"type": "function"}],
    }


def test_gateway_embeddings_go_to_embedding_server(monkeypatch):
    module = _selfhosted(monkeypatch)
    url, body = module.rewrite_gateway_request(
        "/v1/embeddings", {"model": "gemini-embedding-001", "input": ["a"], "dimensions": 3072}
    )
    assert url == "http://embed.local:11434/v1/embeddings"
    assert body == {"model": "nomic-embed-text", "input": ["a"]}
