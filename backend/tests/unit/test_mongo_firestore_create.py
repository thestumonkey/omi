"""MongoFirestore create(): Firestore's insert-only write. [fork-only]

Needs a MongoDB replica set; set MONGO_FIRESTORE_TEST_URL, e.g.
``mongodb://localhost:37017/?directConnection=true`` after
``docker run -d -p 37017:27017 mongo:7 --replSet rs0`` + ``rs.initiate()``.
"""

import os
import uuid

import pytest
from google.api_core.exceptions import Conflict
from google.cloud import firestore

TEST_URL = os.environ.get("MONGO_FIRESTORE_TEST_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="MONGO_FIRESTORE_TEST_URL not set")


@pytest.fixture
def db():
    from database.mongo_firestore import MongoFirestore

    name = f"shim_create_{uuid.uuid4().hex[:8]}"
    store = MongoFirestore(TEST_URL, name)
    yield store
    store._client.drop_database(name)


def test_create_writes_new_document(db):
    ref = db.collection("users").document("u1").collection("sessions").document("s1")
    ref.create({"state": "open", "count": firestore.Increment(1)})
    assert ref.get().to_dict() == {"state": "open", "count": 1}


def test_create_refuses_existing_document(db):
    ref = db.collection("users").document("u1")
    ref.create({"name": "a"})
    with pytest.raises(Conflict):
        ref.create({"name": "b"})
    assert ref.get().to_dict() == {"name": "a"}


def test_batch_create(db):
    ref = db.collection("things").document("t1")
    batch = db.batch()
    batch.create(ref, {"v": 1})
    batch.commit()
    assert ref.get().to_dict() == {"v": 1}


def test_create_inside_googles_transactional_decorator(db):
    # The upstream idiom that failed in production: read, then create if missing.
    ref = db.collection("recording_sessions").document("s1")

    @firestore.transactional
    def create_or_get(transaction):
        snapshot = ref.get(transaction=transaction)
        if snapshot.exists:
            return snapshot.to_dict()
        current = {"conversation_id": "c1"}
        transaction.create(ref, current)
        return current

    assert create_or_get(db.transaction()) == {"conversation_id": "c1"}
    assert create_or_get(db.transaction()) == {"conversation_id": "c1"}
