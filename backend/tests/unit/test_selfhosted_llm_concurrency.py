"""Concurrency limiting for the self-hosted LLM client.

A self-hosted llama.cpp/lemonade backend serves with `--parallel 1`. Conversation
post-processing fans out four LLM extractions at once onto a 24-worker pool, so
without a limit a handful of conversations put a dozen-plus concurrent requests into
a server that can run one. These tests pin the limiting behaviour.
"""

import importlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _reload_clients(monkeypatch, **env):
    """Re-import clients.py with the given env, since the knobs are module-level."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import utils.llm.clients as clients

    return importlib.reload(clients)


class _ConcurrencyProbe(BaseHTTPRequestHandler):
    """Records peak simultaneous in-flight requests."""

    lock = threading.Lock()
    current = 0
    peak = 0
    served = 0

    def do_POST(self):
        with _ConcurrencyProbe.lock:
            _ConcurrencyProbe.current += 1
            _ConcurrencyProbe.peak = max(_ConcurrencyProbe.peak, _ConcurrencyProbe.current)
        time.sleep(0.15)  # hold the "lane" long enough for overlap to be observable
        with _ConcurrencyProbe.lock:
            _ConcurrencyProbe.current -= 1
            _ConcurrencyProbe.served += 1
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request logging
        pass


@pytest.fixture
def probe_server():
    _ConcurrencyProbe.current = 0
    _ConcurrencyProbe.peak = 0
    _ConcurrencyProbe.served = 0
    server = HTTPServer(("127.0.0.1", 0), _ConcurrencyProbe)
    # Threaded accept loop so the server itself never serialises the requests —
    # any serialisation observed must come from the client.
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_limit_of_one_serialises_concurrent_callers(monkeypatch, probe_server):
    """Ten threads sharing the client must never overlap at the server."""
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="1",
    )
    http_client = clients._get_selfhosted_http_client()
    assert http_client is not None

    def hit(_):
        return http_client.post(f"{probe_server}/v1/chat/completions", json={}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(hit, range(10)))

    assert codes == [200] * 10, "every request must complete, not fail"
    assert _ConcurrencyProbe.peak == 1, f"expected no overlap, saw {_ConcurrencyProbe.peak} in flight"
    assert _ConcurrencyProbe.served == 10


def test_surplus_requests_wait_rather_than_fail(monkeypatch, probe_server):
    """Backend post-processing does not regenerate, so queued work must wait, not shed.

    httpx's default 5s pool timeout would fail these; the explicit `pool=None` is what
    makes them wait.
    """
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="1",
    )
    http_client = clients._get_selfhosted_http_client()

    # 8 requests x 0.15s each, serialised, comfortably exceeds httpx's 5s pool default.
    def hit(_):
        return http_client.post(f"{probe_server}/v1/chat/completions", json={}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(hit, range(8)))

    assert all(c == 200 for c in codes), "queued requests must not be shed"


def test_higher_limit_allows_real_parallelism(monkeypatch, probe_server):
    """The gate is a limiter, not a lock — raising it must actually parallelise."""
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="4",
    )
    http_client = clients._get_selfhosted_http_client()

    def hit(_):
        return http_client.post(f"{probe_server}/v1/chat/completions", json={}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(hit, range(8)))

    assert _ConcurrencyProbe.peak > 1, "should genuinely run in parallel"
    assert _ConcurrencyProbe.peak <= 4, f"must not exceed the configured limit, saw {_ConcurrencyProbe.peak}"


def test_zero_disables_the_limit(monkeypatch, probe_server):
    """Cloud providers are elastic — the limit must be opt-out."""
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="0",
    )
    assert clients._get_selfhosted_http_client() is None


def test_client_is_shared_across_calls(monkeypatch, probe_server):
    """A per-call client would give every caller its own pool and no limit at all."""
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="1",
    )
    assert clients._get_selfhosted_http_client() is clients._get_selfhosted_http_client()


def test_read_timeout_is_generous_enough_for_cold_starts(monkeypatch, probe_server):
    """Supplying our own client bypasses langchain's request_timeout, so the httpx
    timeout must cover lemonade's slow generations itself."""
    clients = _reload_clients(
        monkeypatch,
        SELF_HOSTED_LLM_URL=probe_server,
        SELF_HOSTED_LLM_MAX_CONCURRENCY="1",
    )
    timeout = clients._get_selfhosted_http_client().timeout
    assert timeout.read >= 300, f"read timeout {timeout.read} too short for local inference"
    assert timeout.pool is None, "pool timeout must be unbounded so queued work waits"
