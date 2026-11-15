"""Self-hosted notes loop deadline. [fork-only]

A local model on one lane needs more than upstream's 3 x 60s cloud budget for a
long conversation; the loop timed out and the conversation stayed "processing".
"""

import importlib


def _reload(monkeypatch, **env):
    for name in ('SELF_HOSTED_LLM_URL', 'SELF_HOSTED_LLM_READ_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import utils.llm.selfhosted as selfhosted
    import utils.llm.conversation_processing as cp

    importlib.reload(selfhosted)
    return importlib.reload(cp)


def test_self_hosted_loop_follows_read_timeout(monkeypatch):
    cp = _reload(monkeypatch, SELF_HOSTED_LLM_URL='http://127.0.0.1:1/v1', SELF_HOSTED_LLM_READ_TIMEOUT='300')
    assert cp.NOTES_LOOP_DEADLINE_SECONDS == 900
    assert cp.notes_mount().budget.deadline_seconds == 900


def test_short_read_timeout_keeps_upstream_budget(monkeypatch):
    cp = _reload(monkeypatch, SELF_HOSTED_LLM_URL='http://127.0.0.1:1/v1', SELF_HOSTED_LLM_READ_TIMEOUT='10')
    assert cp.NOTES_LOOP_DEADLINE_SECONDS == 3 * cp.CONVERSATION_STRUCTURE_TIMEOUT_SECONDS


def test_upstream_deploy_unchanged(monkeypatch):
    cp = _reload(monkeypatch)
    assert cp.NOTES_LOOP_DEADLINE_SECONDS == 3 * cp.CONVERSATION_STRUCTURE_TIMEOUT_SECONDS
    _reload(monkeypatch)
