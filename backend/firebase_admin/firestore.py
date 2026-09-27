"""``firebase_admin.firestore`` stand-in. [fork-only]

The real module re-exports ``google.cloud.firestore`` and adds ``client()``.
We keep the re-export (value types such as ``ArrayUnion`` and ``Increment`` are
plain Python and need no Google connection) and point ``client()`` at the same
MongoDB-backed client the rest of the backend uses.
"""

from google.cloud.firestore import *  # noqa: F401,F403
from google.cloud import firestore as _gcf

# `from firebase_admin import firestore; firestore.firestore.ArrayUnion` is a
# real upstream idiom (the SDK exposes the google module under this name).
firestore = _gcf


def client(app: object = None, database_id: object = None):
    from database._client import get_firestore_client

    return get_firestore_client()
