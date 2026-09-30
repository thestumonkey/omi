"""Chat model picked from the app's settings. [fork-only]"""

import importlib
import os

os.environ.setdefault("ENCRYPTION_SECRET", "omi_test_secret_for_selfhosted_settings_unit_test")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def selfhosted(monkeypatch):
    monkeypatch.setenv("SELF_HOSTED_LLM_URL", "http://llm.local/v1")
    monkeypatch.setenv("SELF_HOSTED_LLM_MODEL", "llama-default")
    import utils.llm.selfhosted as module

    module = importlib.reload(module)
    store = {}
    monkeypatch.setattr(module, "_read_saved_model", lambda: store.get("model"))
    monkeypatch.setattr(module, "_settings_ref", lambda: type("Ref", (), {"set": lambda _s, d: store.update(d)})())
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
