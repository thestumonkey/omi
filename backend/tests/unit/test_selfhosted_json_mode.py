"""SelfHostedChatOpenAI turns on JSON mode only for prompts that ask for JSON. [fork-only]"""

import json

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
        "max_tokens": 8192,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_gateway_embeddings_go_to_embedding_server(monkeypatch):
    module = _selfhosted(monkeypatch)
    url, body = module.rewrite_gateway_request(
        "/v1/embeddings", {"model": "gemini-embedding-001", "input": ["a"], "dimensions": 3072}
    )
    assert url == "http://embed.local:11434/v1/embeddings"
    assert body == {"model": "nomic-embed-text", "input": ["a"]}


# ── Repair of replies that ignore JSON mode ──────────────────────────────────


class _Items(BaseModel):
    action_items: list[_Title]


def _roundtrip(model, reply):
    llm = _llm()
    instructions = PydanticOutputParser(pydantic_object=model).get_format_instructions()
    llm._get_request_payload([SystemMessage(instructions), HumanMessage("go")])
    return llm._create_chat_result(_response(reply)).generations[0].message.content


def test_unquoted_emoji_value_is_quoted():
    fixed = _roundtrip(_Title, '{\n  "emoji": 📝,\n  "title": "Errands"\n}')
    assert json.loads(fixed) == {"emoji": "📝", "title": "Errands"}


def test_bare_list_is_wrapped_for_single_list_schema():
    fixed = _roundtrip(_Items, '[{"title": "Buy milk"}]')
    assert PydanticOutputParser(pydantic_object=_Items).parse(fixed).action_items[0].title == "Buy milk"


def test_prose_around_object_is_dropped():
    assert json.loads(_roundtrip(_Title, 'Sure! {"title": "Standup"} Hope that helps.')) == {"title": "Standup"}


def test_bare_list_is_not_wrapped_for_multi_field_schema():
    assert _roundtrip(_Title, '[{"title": "x"}]') == '[{"title": "x"}]'


def test_plain_chat_reply_is_never_repaired():
    llm = _llm()
    llm._get_request_payload([HumanMessage("how was my day?")])
    reply = 'You said: 📝, then left.'
    assert llm._create_chat_result(_response(reply)).generations[0].message.content == reply


def test_thinking_is_off_by_default():
    payload = _llm()._get_request_payload([HumanMessage("hi")])
    assert payload["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_reply_length_cap_is_raised_unless_caller_sets_one():
    assert _llm()._get_request_payload([HumanMessage("hi")])["max_tokens"] == 8192
    capped = SelfHostedChatOpenAI(model="llama", api_key="k", base_url="http://localhost:9/v1", max_tokens=100)
    payload = capped._get_request_payload([HumanMessage("hi")])
    assert (payload.get("max_tokens") or payload.get("max_completion_tokens")) == 100
