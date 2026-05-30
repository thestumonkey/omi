import hashlib
import os
import uuid

# Storage seam: a Firestore-API-compatible client backed by MongoDB.
# Every database/*.py module does `from ._client import db`, so this single
# line repoints the entire data layer off Firestore. See mongo_firestore.py.
from database.mongo_firestore import MongoFirestore

db = MongoFirestore(
    os.environ.get('MONGODB_URL', 'mongodb://localhost:27017'),
    os.environ.get('MONGODB_DB', 'omi'),
)


def get_users_uid():
    users_ref = db.collection('users')
    return [str(doc.id) for doc in users_ref.stream()]


def document_id_from_seed(seed: str) -> uuid.UUID:
    """Avoid repeating the same data"""
    seed_hash = hashlib.sha256(seed.encode('utf-8')).digest()
    generated_uuid = uuid.UUID(bytes=seed_hash[:16], version=4)
    return str(generated_uuid)
