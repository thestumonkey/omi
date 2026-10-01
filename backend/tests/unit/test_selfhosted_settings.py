"""Server-wide settings changed from the app: chat model and live voice. [fork-only]"""

import asyncio
import importlib
import os

os.environ.setdefault("ENCRYPTION_SECRET", "omi_test_secret_for_selfhosted_settings_unit_test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def store(monkeypatch):
    """In-memory selfhosted_settings collection behind utils.selfhosted_config."""
    from utils import selfhosted_config

    docs = {}

    class _Ref:
        def __init__(self, name):
            self.name = name

        def set(self, data):
            docs[self.name] = dict(data)

    monkeypatch.setattr(selfhosted_config, "_cache", {})
    monkeypatch.setattr(selfhosted_config, "_load", lambda name: dict(docs.get(name, {})))
    monkeypatch.setattr(selfhosted_config, "_ref", _Ref)
    return docs


@pytest.fixture
def selfhosted(monkeypatch, store):
    monkeypatch.setenv("SELF_HOSTED_LLM_URL", "http://llm.local/v1")
    monkeypatch.setenv("SELF_HOSTED_LLM_MODEL", "llama-default")
    import utils.llm.selfhosted as module

    module = importlib.reload(module)
    monkeypatch.setattr(module, "list_models", lambda: ["llama-default", "qwen3-8b"])
    return module


def test_default_model_until_one_is_saved(selfhosted):
    assert selfhosted.current_model() == "llama-default"
    selfhosted.save_model("qwen3-8b")
    assert selfhosted.current_model() == "qwen3-8b"
    assert selfhosted.rewrite_gateway_request("/v1/chat/completions", {"messages": []})[1]["model"] == "qwen3-8b"
    selfhosted.save_model(None)
    assert selfhosted.current_model() == "llama-default"


def test_client_follows_saved_model(selfhosted):
    assert selfhosted.get_selfhosted_llm().model_name == "llama-default"
    selfhosted.save_model("qwen3-8b")
    assert selfhosted.get_selfhosted_llm().model_name == "qwen3-8b"


@pytest.fixture
def api(selfhosted):
    from routers import selfhosted_settings
    from utils.other import endpoints as auth

    app = FastAPI()
    app.include_router(selfhosted_settings.router)
    app.dependency_overrides[auth.get_current_user_uid] = lambda: "omi/alice"
    return TestClient(app)


def test_api_lists_and_sets_model(api):
    body = api.get("/v1/selfhosted/llm").json()
    assert body["model"] == "llama-default" and body["models"] == ["llama-default", "qwen3-8b"]
    assert api.post("/v1/selfhosted/llm", json={"model": "qwen3-8b"}).json()["model"] == "qwen3-8b"
    assert api.post("/v1/selfhosted/llm", json={"model": None}).json()["model"] == "llama-default"


def test_api_rejects_unknown_model(api):
    assert api.post("/v1/selfhosted/llm", json={"model": "gpt-5"}).status_code == 400


# ── Live voice ───────────────────────────────────────────────────────────────


def test_voice_defaults_off_when_self_hosted(monkeypatch, store):
    from utils import selfhosted_config

    monkeypatch.setenv("SELF_HOSTED", "true")
    assert selfhosted_config.voice_mode() == "off"
    monkeypatch.delenv("SELF_HOSTED")
    selfhosted_config._cache.clear()
    assert selfhosted_config.voice_mode() == "cloud"


def test_api_sets_voice_mode(monkeypatch, api):
    monkeypatch.setenv("SELF_HOSTED", "true")
    assert api.get("/v1/selfhosted/voice").json() == {"mode": "off"}
    assert api.post("/v1/selfhosted/voice", json={"mode": "cloud"}).json() == {"mode": "cloud"}
    assert api.post("/v1/selfhosted/voice", json={"mode": "loud"}).status_code == 422


def test_voice_off_blocks_token_mint_and_relay(monkeypatch, store):
    from routers import desktop_realtime, omni_relay

    monkeypatch.setenv("SELF_HOSTED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    data, error = asyncio.run(desktop_realtime._post_json("https://api.openai.com/x", "openai", {}, {}))
    assert data is None and error.status_code == 503
    assert omni_relay._upstream("openai", None) == (None, "live voice is off on this server")


def test_voice_cloud_uses_providers(monkeypatch, store):
    from routers import omni_relay
    from utils import selfhosted_config

    monkeypatch.setenv("SELF_HOSTED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    selfhosted_config.write("voice", {"mode": "cloud"})
    (url, headers), err = omni_relay._upstream("openai", None)
    assert err is None and "api.openai.com" in url
