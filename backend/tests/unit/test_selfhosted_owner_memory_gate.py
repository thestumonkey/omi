"""Self-hosted memories need the owner to have spoken. [fork-only]"""

from types import SimpleNamespace

from utils.conversations import owner_memory_gate as gate


def _seg(text, speaker_id, is_user=False):
    return SimpleNamespace(text=text, speaker_id=speaker_id, speaker_id_scope=None, is_user=is_user)


TV = [_seg("tonight on the news the weather will be " * 3, 0), _seg("and now the sport results " * 3, 1)]


def test_tv_only_conversation_is_rejected():
    presence = gate.owner_presence(TV, min_words=20)
    assert not presence.admitted
    assert presence.reason == "owner_no_owner"


def test_no_speaker_ids_is_rejected():
    segs = [SimpleNamespace(text="hello there " * 20, speaker_id=None, is_user=False)]
    assert gate.owner_presence(segs, min_words=20).reason == "owner_no_speaker_ids"


def test_owner_one_line_over_tv_is_too_quiet():
    presence = gate.owner_presence(TV + [_seg("turn it up", 2, is_user=True)], min_words=20)
    assert not presence.admitted
    assert presence.reason == "owner_too_quiet"
    assert presence.owner_words == 3


def test_owner_talking_is_admitted():
    segs = TV + [_seg("I am flying to Lisbon next Tuesday to see my sister and her kids", 2, is_user=True)] * 2
    assert gate.owner_presence(segs, min_words=20).admitted


def test_two_owner_clusters_count_together():
    # Live and uploaded-audio transcripts label the owner SPEAKER_0 and SPEAKER_00.
    segs = [_seg("word " * 12, 0, is_user=True), _seg("word " * 12, 1, is_user=True)]
    presence = gate.owner_presence(segs, min_words=20)
    assert presence.admitted and presence.owner_words == 24
    assert not gate.owner_presence(segs[:1], min_words=20).admitted


def test_gate_defaults_follow_self_hosted(monkeypatch):
    monkeypatch.delenv("SELF_HOSTED_MEMORY_OWNER_GATE", raising=False)
    monkeypatch.setenv("SELF_HOSTED", "true")
    assert gate.gate_enabled()
    monkeypatch.setenv("SELF_HOSTED_MEMORY_OWNER_GATE", "off")
    assert not gate.gate_enabled()
    monkeypatch.delenv("SELF_HOSTED_MEMORY_OWNER_GATE")
    monkeypatch.delenv("SELF_HOSTED")
    assert not gate.gate_enabled()


def test_extract_memories_inner_skips_when_owner_absent(monkeypatch):
    import utils.conversations.process_conversation as pc

    calls = []
    monkeypatch.setattr(pc, "_upstream_extract_memories_inner", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("SELF_HOSTED_MEMORY_OWNER_GATE", "on")
    _voice_profile(monkeypatch, True)
    conversation = SimpleNamespace(id="c1", transcript_segments=TV, source="omi")
    result = pc._extract_memories_inner("u1", conversation)
    assert result.count == 0 and result.path == "owner_gate"
    assert calls == []

    owner = [_seg("I am flying to Lisbon next Tuesday to see my sister and her kids", 2, is_user=True)] * 2
    pc._extract_memories_inner("u1", SimpleNamespace(id="c2", transcript_segments=TV + owner, source="omi"))
    assert len(calls) == 1


def _voice_profile(monkeypatch, present):
    import utils.other.storage as storage

    if isinstance(present, Exception):

        def has(uid):
            raise present

    else:

        def has(uid):
            return present

    monkeypatch.setattr(storage, "get_user_has_speech_profile", has)


def _conv_gate(monkeypatch):
    import utils.conversations.process_conversation as pc

    calls = []
    monkeypatch.setattr(pc, "_upstream_get_structured", lambda *a, **k: (calls.append(a), (pc.Structured(), False))[1])
    monkeypatch.setattr(pc, "is_release_probe_uid", lambda uid: False)
    monkeypatch.setenv("SELF_HOSTED_MEMORY_OWNER_GATE", "on")
    _voice_profile(monkeypatch, True)
    return pc, calls


def test_tv_conversation_is_discarded_without_llm_call(monkeypatch):
    pc, calls = _conv_gate(monkeypatch)
    decisions = []
    structured, discarded = pc._get_structured(
        "u1", "en", SimpleNamespace(id="c1", transcript_segments=TV), relevance_observer=decisions.append
    )
    assert discarded is True
    assert calls == []
    assert decisions[0].verdict == "discard" and decisions[0].reason == "owner_no_owner"


def test_owner_conversation_and_kept_conversation_reach_upstream(monkeypatch):
    pc, calls = _conv_gate(monkeypatch)
    owner = [_seg("I am flying to Lisbon next Tuesday to see my sister and her kids", 2, is_user=True)] * 2
    assert pc._get_structured("u1", "en", SimpleNamespace(id="c2", transcript_segments=TV + owner))[1] is False
    assert pc._get_structured("u1", "en", SimpleNamespace(id="c3", transcript_segments=TV), user_kept=True)[1] is False
    recovery = pc.ProcessingTrigger.SERVER_RECOVERY
    assert (
        pc._get_structured("u1", "en", SimpleNamespace(id="c4", transcript_segments=TV), trigger=recovery)[1] is False
    )
    assert len(calls) == 3


def test_conversation_gate_off_reaches_upstream(monkeypatch):
    pc, calls = _conv_gate(monkeypatch)
    monkeypatch.setenv("SELF_HOSTED_MEMORY_OWNER_GATE", "off")
    assert pc._get_structured("u1", "en", SimpleNamespace(id="c5", transcript_segments=TV))[1] is False
    assert len(calls) == 1


def test_no_voice_profile_hides_nothing(monkeypatch):
    # Speaker ID cannot find the owner without a Voice Profile, so the rule
    # would hide every recording. It must stay open.
    pc, calls = _conv_gate(monkeypatch)
    _voice_profile(monkeypatch, False)
    assert pc._get_structured("u1", "en", SimpleNamespace(id="c6", transcript_segments=TV))[1] is False
    assert len(calls) == 1
    memory_calls = []
    monkeypatch.setattr(pc, "_upstream_extract_memories_inner", lambda *a, **k: memory_calls.append(a))
    pc._extract_memories_inner("u1", SimpleNamespace(id="c7", transcript_segments=TV, source="omi"))
    assert len(memory_calls) == 1


def test_voice_profile_lookup_error_hides_nothing(monkeypatch):
    pc, calls = _conv_gate(monkeypatch)
    _voice_profile(monkeypatch, RuntimeError("storage down"))
    assert pc._get_structured("u1", "en", SimpleNamespace(id="c8", transcript_segments=TV))[1] is False
    assert len(calls) == 1
