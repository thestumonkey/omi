"""MongoFirestore refs have .parent; snapshots return plain int. [fork-only]

Two sync failures came from these gaps:
- user_usage walks `usage_ref.parent.parent` to the user document.
- pinecone_partition_state checks `type(row['latest_write_lsn']) is int`.
  pymongo returns big integers as bson.Int64, so the check failed.

Needs no database: refs are plain objects, and snapshots take a raw dict.
"""

from bson.int64 import Int64

from database.mongo_firestore import CollectionReference, DocumentReference, DocumentSnapshot


def test_document_parent_is_its_collection():
    ref = DocumentReference(None, 'users/u1/hourly_usage/2026-11-14-10')
    coll = ref.parent
    assert isinstance(coll, CollectionReference)
    assert coll.path == 'users/u1/hourly_usage' and coll.id == 'hourly_usage'
    user = coll.parent
    assert isinstance(user, DocumentReference) and user.path == 'users/u1' and user.id == 'u1'


def test_top_level_collection_has_no_parent():
    assert CollectionReference(None, 'users').parent is None
    assert DocumentReference(None, 'users/u1').parent.parent is None


def test_snapshot_returns_plain_int_for_int64():
    raw = {'_id': 'r/1', 'n': Int64(1794650082513657734), 'nested': {'m': Int64(5)}, 'xs': [Int64(7), {'k': Int64(8)}]}
    row = DocumentSnapshot(DocumentReference(None, 'r/1'), raw).to_dict()
    assert type(row['n']) is int and row['n'] == 1794650082513657734
    assert type(row['nested']['m']) is int
    assert [type(row['xs'][0]), type(row['xs'][1]['k'])] == [int, int]


def test_snapshot_keeps_other_types():
    raw = {'_id': 'r/1', 'b': True, 's': 'x', 'f': 1.5, 'none': None}
    row = DocumentSnapshot(DocumentReference(None, 'r/1'), raw).to_dict()
    assert row == {'b': True, 's': 'x', 'f': 1.5, 'none': None}
    assert type(row['b']) is bool
