"""MongoFirestore enforces Firestore write preconditions (option=). [fork-only]

Upstream uses `option=LastUpdateOption(snapshot.update_time)` as a
compare-and-set in about 20 places (outbox_cas, chat, conversations, ...).
The shim must refuse the write when the document changed since the read, and
fail closed on a precondition it cannot check.

Needs a MongoDB replica set; set MONGO_FIRESTORE_TEST_URL, e.g.
``mongodb://localhost:37017/?directConnection=true`` after
``docker run -d -p 37017:27017 mongo:7 --replSet rs0`` + ``rs.initiate()``.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore

TEST_URL = os.environ.get("MONGO_FIRESTORE_TEST_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="MONGO_FIRESTORE_TEST_URL not set")


@pytest.fixture
def db():
    from database.mongo_firestore import MongoFirestore

    name = f"shim_cas_{uuid.uuid4().hex[:8]}"
    store = MongoFirestore(TEST_URL, name)
    yield store
    store._client.drop_database(name)


@pytest.fixture
def ref(db):
    r = db.collection("users").document("u1").collection("outbox").document("o1")
    r.set({"n": 1})
    return r


def test_snapshot_has_update_time_and_hides_revision(ref):
    snap = ref.get()
    assert snap.update_time is not None
    assert "_ut" not in snap.to_dict()


def test_each_write_gets_a_new_update_time(ref):
    first = ref.get().update_time
    ref.update({"n": 2})
    assert ref.get().update_time._rev > first._rev


def test_update_with_fresh_read_succeeds(db, ref):
    snap = ref.get()
    ref.update({"n": 2}, option=db.write_option(last_update_time=snap.update_time))
    assert ref.get().to_dict() == {"n": 2}


def test_update_after_a_concurrent_write_is_refused(db, ref):
    stale = ref.get()
    ref.update({"n": 99})  # another writer
    with pytest.raises(FailedPrecondition):
        ref.update({"n": 2}, option=firestore.LastUpdateOption(stale.update_time))
    assert ref.get().to_dict() == {"n": 99}


def test_delete_after_a_concurrent_write_is_refused(db, ref):
    stale = ref.get()
    ref.set({"n": 5}, merge=True)
    with pytest.raises(FailedPrecondition):
        ref.delete(option=db.write_option(last_update_time=stale.update_time))
    assert ref.get().exists


def test_delete_with_fresh_read_succeeds(db, ref):
    ref.delete(option=db.write_option(last_update_time=ref.get().update_time))
    assert not ref.get().exists


def test_foreign_timestamp_fails_closed(db, ref):
    with pytest.raises(FailedPrecondition):
        ref.update({"n": 2}, option=db.write_option(last_update_time=datetime.now(timezone.utc)))
    assert ref.get().to_dict() == {"n": 1}


def test_unknown_option_fails_closed(ref):
    with pytest.raises(FailedPrecondition):
        ref.update({"n": 2}, option=object())


def test_exists_options(db, ref):
    missing = ref.parent.document("nope") if hasattr(ref, "parent") else None
    missing = missing or db.collection("users").document("u1").collection("outbox").document("nope")
    with pytest.raises(FailedPrecondition):
        ref.delete(option=db.write_option(exists=False))
    with pytest.raises(FailedPrecondition):
        missing.delete(option=db.write_option(exists=True))
    with pytest.raises(NotFound):
        missing.update({"n": 1})


def test_batch_and_transaction_pass_the_option(db, ref):
    stale = ref.get()
    ref.update({"n": 7})
    batch = db.batch()
    batch.update(ref, {"n": 8}, option=db.write_option(last_update_time=stale.update_time))
    with pytest.raises(FailedPrecondition):
        batch.commit()

    @firestore.transactional
    def cas(txn):
        txn.update(ref, {"n": 9}, option=db.write_option(last_update_time=stale.update_time))

    with pytest.raises(FailedPrecondition):
        cas(db.transaction())
    assert ref.get().to_dict() == {"n": 7}


def test_legacy_document_without_revision(db):
    # A document written before revisions existed has no update_time.
    r = db.collection("legacy").document("d1")
    r._mc().insert_one({"_id": r.path, "_p": "legacy", "_k": "d1", "n": 1})
    snap = r.get()
    assert snap.update_time is None
    r.update({"n": 2}, option=firestore.LastUpdateOption(snap.update_time))
    with pytest.raises(FailedPrecondition):  # now it has a revision
        r.update({"n": 3}, option=firestore.LastUpdateOption(None))
    assert r.get().to_dict() == {"n": 2}


def test_select_keeps_update_time(db, ref):
    (snap,) = list(db.collection("users").document("u1").collection("outbox").select(["n"]).stream())
    assert snap.update_time is not None


def test_plain_datetime_copy_of_update_time_still_matches(db, ref):
    # conversations.py copies update_time into updated_at; a model round trip
    # turns it into a plain datetime. It must still work as the lock value.
    t = ref.get().update_time
    plain = datetime(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, tzinfo=timezone.utc)
    ref.update({"n": 2}, option=db.write_option(last_update_time=plain))
    with pytest.raises(FailedPrecondition):  # the same value is stale now
        ref.update({"n": 3}, option=db.write_option(last_update_time=plain))


def test_naive_datetime_fails_closed(db, ref):
    with pytest.raises(FailedPrecondition):
        ref.update({"n": 2}, option=db.write_option(last_update_time=datetime.now()))


def test_writes_return_update_time(db, ref):
    result = ref.update({"n": 2}, option=db.write_option(last_update_time=ref.get().update_time))
    assert result.update_time == ref.get().update_time
    # The returned time works as the next lock value (claim_record_lane_owner).
    ref.update({"n": 3}, option=db.write_option(last_update_time=result.update_time))
    assert ref.set({"n": 4}).update_time == ref.get().update_time
    batch = db.batch()
    batch.update(ref, {"n": 5})
    (written,) = batch.commit()
    assert written.update_time == ref.get().update_time
