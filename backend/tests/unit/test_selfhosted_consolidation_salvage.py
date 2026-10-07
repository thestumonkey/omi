"""Self-hosted: one invalid consolidation decision no longer voids the batch. [fork-only]"""

import json

import utils.memory.canonical_consolidation as cc

RAW = json.dumps(
    {
        "decisions": [
            {"source_memory_id": "mem_a", "route": "reject", "rationale": "ephemeral"},
            # The slip seen on a local model: target equals source.
            {"source_memory_id": "mem_b", "route": "archive", "target_memory_id": "mem_b", "rationale": "x"},
        ],
        "recurrence_signals": [{"not": "a signal"}],
        "reasoning": "batch note",
    }
)


def _context():
    from types import SimpleNamespace

    return SimpleNamespace(uid="u1")


def test_invalid_decision_becomes_review(monkeypatch):
    monkeypatch.setenv("SELF_HOSTED", "true")
    monkeypatch.setattr(cc, "build_consolidation_llm_messages", lambda context: [])
    batch = cc.invoke_consolidation_agent(_context(), llm_invoke=lambda messages: "```json\n" + RAW + "\n```")
    assert [(d.source_memory_id, d.route) for d in batch.decisions] == [("mem_a", "reject"), ("mem_b", "review")]
    assert batch.recurrence_signals == []
    assert batch.reasoning == "batch note"


def test_upstream_behaviour_without_self_hosted(monkeypatch):
    monkeypatch.delenv("SELF_HOSTED", raising=False)
    monkeypatch.setattr(cc, "build_consolidation_llm_messages", lambda context: [])
    batch = cc.invoke_consolidation_agent(_context(), llm_invoke=lambda messages: RAW)
    assert batch.decisions == [] and batch.reasoning.startswith("parse_failed:")


def test_unusable_output_still_fails(monkeypatch):
    monkeypatch.setenv("SELF_HOSTED", "true")
    monkeypatch.setattr(cc, "build_consolidation_llm_messages", lambda context: [])
    batch = cc.invoke_consolidation_agent(_context(), llm_invoke=lambda messages: "not json at all")
    assert batch.reasoning.startswith("parse_failed:")
