"""MongoFirestore: Increment and friends inside nested maps. [fork-only]

`llm_usage.record_llm_usage` writes `set(_nested({'a.b.c': Increment(n)}), merge=True)`.
Needs MONGO_FIRESTORE_TEST_URL (see test_mongo_firestore_create.py).
"""

import os
import uuid

import pytest
from google.cloud import firestore

TEST_URL = os.environ.get("MONGO_FIRESTORE_TEST_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="MONGO_FIRESTORE_TEST_URL not set")


@pytest.fixture
def ref():
    from database.mongo_firestore import MongoFirestore

    name = f"shim_nested_{uuid.uuid4().hex[:8]}"
    store = MongoFirestore(TEST_URL, name)
    yield store.collection("users").document("u1").collection("llm_usage").document("2026-09-30")
    store._client.drop_database(name)


def test_merge_set_increments_nested_counter_and_keeps_siblings(ref):
    ref.set({"chat": {"llama": {"calls": firestore.Increment(1), "keep": "x"}}}, merge=True)
    ref.set({"chat": {"llama": {"calls": firestore.Increment(2)}, "other": {"calls": 5}}}, merge=True)
    assert ref.get().to_dict() == {"chat": {"llama": {"calls": 3, "keep": "x"}, "other": {"calls": 5}}}


def test_plain_set_replaces_map_then_applies_nested_counter(ref):
    ref.set({"chat": {"old": 1}})
    ref.set({"chat": {"llama": {"calls": firestore.Increment(4)}, "note": "n"}})
    assert ref.get().to_dict() == {"chat": {"llama": {"calls": 4}, "note": "n"}}


def test_update_with_nested_counter(ref):
    ref.set({"chat": {"calls": 1}})
    ref.update({"chat": {"calls": firestore.Increment(1), "tag": "t"}, "top": firestore.Increment(2)})
    assert ref.get().to_dict() == {"chat": {"tag": "t", "calls": 1}, "top": 2}


def test_collection_group_skips_rows_in_another_layout(ref):
    ref.set({"n": 1})
    store = ref._store
    store._db[store._safe("llm_usage")].insert_one({"_id": "legacy-row", "uid": "u0", "n": 2})
    rows = [s.to_dict() for s in store.collection_group("llm_usage").stream()]
    assert rows == [{"n": 1}]


def test_update_dotted_path_under_null_parent(ref):
    # conversations: speaker_resolution is null, then upstream writes
    # 'speaker_resolution.participant_speaker_ids'. Firestore turns it into a map.
    ref.set({"speaker_resolution": None, "keep": 1})
    ref.update({"speaker_resolution.participant_speaker_ids": ["s1"]})
    assert ref.get().to_dict() == {"speaker_resolution": {"participant_speaker_ids": ["s1"]}, "keep": 1}


def test_update_dotted_path_under_scalar_and_missing_parents(ref):
    ref.set({"a": "text"})
    ref.update({"a.b.c": 1, "x.y": 2})
    assert ref.get().to_dict() == {"a": {"b": {"c": 1}}, "x": {"y": 2}}


def test_merge_set_into_null_parent(ref):
    ref.set({"chat": None})
    ref.set({"chat": {"calls": firestore.Increment(1)}}, merge=True)
    assert ref.get().to_dict() == {"chat": {"calls": 1}}


def test_order_and_page_by_document_id(ref):
    from google.cloud import firestore as gf

    items = ref._store.collection("users").document("u1").collection("memory_items")
    for doc_id in ("a", "b", "c"):
        items.document(doc_id).set({"updated_at": 1})
    q = items.order_by("updated_at", direction=gf.Query.DESCENDING).order_by("__name__")
    assert [s.id for s in q.stream()] == ["a", "b", "c"]
    page2 = q.start_after({"updated_at": 1, "__name__": items.document("a")}).limit(1)
    assert [s.id for s in page2.stream()] == ["b"]
    first = next(iter(q.limit(1).stream()))
    assert [s.id for s in q.start_after(first).stream()] == ["b", "c"]
    assert [s.id for s in items.where("__name__", "==", items.document("c")).stream()] == ["c"]
