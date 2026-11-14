"""MongoFirestore accepts Firestore's retry= and timeout= options. [fork-only]

Upstream passes them on many reads and writes, for example the 3-second bounded
read in utils/memory/memory_system.py. When the shim refused them, every sync
failed at finalize with "canonical apply control state is unreadable".

Checks signatures only, so it needs no MongoDB.
"""

import inspect

import pytest

from database import mongo_firestore as mf

METHODS = [
    (mf.DocumentReference, "get"),
    (mf.DocumentReference, "set"),
    (mf.DocumentReference, "create"),
    (mf.DocumentReference, "update"),
    (mf.DocumentReference, "delete"),
    (mf.Query, "stream"),
    (mf.Query, "get"),
    (mf._CountQuery, "get"),
    (mf.CollectionReference, "add"),
    (mf.WriteBatch, "commit"),
    (mf.Transaction, "get"),
]


@pytest.mark.parametrize("cls,name", METHODS, ids=[f"{c.__name__}.{n}" for c, n in METHODS])
def test_method_accepts_retry_and_timeout(cls, name):
    params = inspect.signature(getattr(cls, name)).parameters
    assert "retry" in params and "timeout" in params


@pytest.mark.parametrize("name", ["update", "delete"])
def test_document_write_accepts_option(name):
    assert "option" in inspect.signature(getattr(mf.DocumentReference, name)).parameters
