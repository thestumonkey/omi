"""SELF_HOSTED_MEMORY_EXPIRY=off skips the Short-term hiding step. [fork-only]"""

import importlib


def test_expiry_off_skips_ttl_lifecycle(monkeypatch):
    import utils.memory.short_term_promotion as module

    monkeypatch.setenv("SELF_HOSTED_MEMORY_EXPIRY", "off")
    module = importlib.reload(module)
    report = module.run_canonical_short_term_ttl_lifecycle("u1", db_client=object(), run_id="r")
    assert report.skipped_reason == "self_hosted_expiry_off"
    assert report.lifecycle_created_count == 0
    monkeypatch.delenv("SELF_HOSTED_MEMORY_EXPIRY")
    module = importlib.reload(module)
    assert module.run_canonical_short_term_ttl_lifecycle.__module__ == module.__name__
    assert "self_hosted" not in (module.run_canonical_short_term_ttl_lifecycle.__doc__ or "")
