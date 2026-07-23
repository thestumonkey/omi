"""
A Firestore-API-compatible client backed by MongoDB.

Lets upstream `backend/database/*.py` run UNCHANGED by swapping only
`_client.py`'s `db = firestore.Client()` for `db = MongoFirestore(...)`.
Every db module does `from ._client import db`, so one seam covers all 32 files.

Mapping model (generic / path-faithful — no per-collection knowledge):
  Each Firestore document is stored as one Mongo document:
    { "_id": "<full firestore path>",   e.g. "users/abc/conversations/123"
      "_p":  "<parent collection path>", e.g. "users/abc/conversations"
      "_k":  "<leaf doc id>",            e.g. "123"
      ...user fields }
  Mongo collection name = the leaf collection name (last path segment). A
  collection query filters on `_p`; a collection_group query does NOT (matches
  every parent). Document _ids are full paths, so hierarchy is exact.

Reuses google's inert *value types* (Increment/ArrayUnion/FieldFilter/
BaseCompositeFilter/Sentinel) via isinstance — it never calls firestore.Client(),
so NO Google credentials / network are involved.
"""

import functools
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import OperationFailure

from google.cloud import firestore as _fs
from google.cloud.firestore_v1.transforms import Increment, ArrayUnion, ArrayRemove
from google.cloud.firestore_v1.base_query import BaseCompositeFilter

DELETE_FIELD = _fs.DELETE_FIELD
SERVER_TIMESTAMP = _fs.SERVER_TIMESTAMP

_RESERVED = ('_id', '_p', '_k')

# Firestore op string -> function producing a Mongo match expression for a field.
_OP = {
    '==': lambda v: {'$eq': v},
    '!=': lambda v: {'$ne': v},
    '<': lambda v: {'$lt': v},
    '<=': lambda v: {'$lte': v},
    '>': lambda v: {'$gt': v},
    '>=': lambda v: {'$gte': v},
    'in': lambda v: {'$in': list(v)},
    'not-in': lambda v: {'$nin': list(v)},
    'array_contains': lambda v: {'$eq': v},  # Mongo equality matches array membership
    'array_contains_any': lambda v: {'$in': list(v)},
}


def _now():
    return datetime.now(timezone.utc)


def _clause_to_mongo(field: str, op: Any, value: Any) -> dict:
    # Unary operators (IS_NULL / IS_NOT_NULL / IS_NAN / IS_NOT_NAN) arrive as an
    # `Operator` enum rather than an op string. Firestore rewrites
    # `where(field, '==', None)` to IS_NULL, so this fires for null-field queries
    # (e.g. messages with no plugin_id). Mongo `{field: None}` matches both an
    # explicit null and a missing field, which is the intended "is null" semantics.
    if op not in _OP:
        # Prefer the enum's .name: in Python 3.11+ str(IntEnum) returns the integer
        # value ('3'), not 'Operator.IS_NULL', so str-parsing alone silently fails.
        op_name = getattr(op, 'name', None) or str(op)
        op_name = op_name.upper().replace('OPERATOR.', '').replace('-', '_')
        if op_name == 'IS_NULL':
            return {field: None}
        if op_name == 'IS_NOT_NULL':
            return {field: {'$ne': None}}
        if op_name == 'IS_NAN':
            return {field: float('nan')}
        if op_name == 'IS_NOT_NAN':
            return {field: {'$ne': float('nan')}}
        raise KeyError(f'unsupported Firestore operator: {op!r}')
    return {field: _OP[op](value)}


def _filter_to_mongo(f) -> dict:
    """Translate a FieldFilter or BaseCompositeFilter into a Mongo expression."""
    if isinstance(f, BaseCompositeFilter):
        op = getattr(f, 'operator', 'AND')
        op = str(op).upper().replace('OPERATOR.', '')
        sub = [_filter_to_mongo(x) for x in f.filters]
        return {'$or': sub} if 'OR' in op else {'$and': sub}
    # FieldFilter
    return _clause_to_mongo(f.field_path, f.op_string, f.value)


def _split_transforms(data: Dict[str, Any]) -> Dict[str, Dict]:
    """Expand Firestore sentinels into a Mongo update spec."""
    spec: Dict[str, Dict] = {}

    def put(op, field, val):
        spec.setdefault(op, {})[field] = val

    for field, val in data.items():
        if isinstance(val, Increment):
            put('$inc', field, val.value)
        elif isinstance(val, ArrayUnion):
            put('$addToSet', field, {'$each': list(val.values)})
        elif isinstance(val, ArrayRemove):
            put('$pull', field, {'$in': list(val.values)})
        elif val is DELETE_FIELD:
            put('$unset', field, '')
        elif val is SERVER_TIMESTAMP:
            put('$set', field, _now())
        else:
            put('$set', field, val)
    return spec


class _AggResult:
    """Mirrors Firestore aggregation result row: rows[0][0].value"""

    def __init__(self, value):
        self.value = value


class DocumentSnapshot:
    def __init__(self, ref: 'DocumentReference', raw: Optional[dict]):
        self.reference = ref
        self.id = ref.id
        self._raw = raw
        self.exists = raw is not None
        self.create_time = None
        self.update_time = None

    def to_dict(self) -> Optional[dict]:
        if self._raw is None:
            return None
        return {k: v for k, v in self._raw.items() if k not in _RESERVED}

    def get(self, field: str):
        return (self.to_dict() or {}).get(field)


class DocumentReference:
    def __init__(self, store: 'MongoFirestore', path: str):
        self._store = store
        self.path = path
        parts = path.split('/')
        self.id = parts[-1]
        self._parent = '/'.join(parts[:-1])
        self._coll_name = parts[-2]

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, other):
        return isinstance(other, DocumentReference) and other.path == self.path

    def _mc(self):
        return self._store._db[self._store._safe(self._coll_name)]

    def collection(self, name: str) -> 'CollectionReference':
        return CollectionReference(self._store, f'{self.path}/{name}')

    def _session(self, transaction):
        return transaction._session if transaction is not None else None

    def set(self, data: dict, merge: bool = False, transaction=None):
        spec = _split_transforms(data)
        base = {'_id': self.path, '_p': self._parent, '_k': self.id}
        s = self._session(transaction)
        if merge:
            spec.setdefault('$set', {}).update(base)
            self._mc().update_one({'_id': self.path}, spec, upsert=True, session=s)
        else:
            doc = dict(base)
            doc.update(spec.get('$set', {}))
            self._mc().replace_one({'_id': self.path}, doc, upsert=True, session=s)

    def update(self, data: dict, transaction=None):
        spec = _split_transforms(data)
        spec.setdefault('$set', {}).update({'_p': self._parent, '_k': self.id})
        res = self._mc().update_one({'_id': self.path}, spec, upsert=False, session=self._session(transaction))
        if res.matched_count == 0:
            from google.api_core.exceptions import NotFound

            raise NotFound(f'No document to update: {self.path}')

    def get(self, field_paths=None, transaction=None) -> DocumentSnapshot:
        # Firestore's DocumentReference.get(field_paths=None, transaction=None):
        # the first positional arg is an optional list of field paths to project.
        projection = None
        if field_paths:
            # always keep the bookkeeping fields so DocumentSnapshot stays well-formed
            projection = {f: 1 for f in field_paths}
            projection.update({'_id': 1, '_p': 1, '_k': 1})
        raw = self._mc().find_one(
            {'_id': self.path}, projection, session=self._session(transaction)
        )
        return DocumentSnapshot(self, raw)

    def delete(self, transaction=None):
        self._mc().delete_one({'_id': self.path}, session=self._session(transaction))


class _CountQuery:
    def __init__(self, query: 'Query'):
        self._query = query

    def get(self, transaction=None):
        q = self._query
        n = q._store._db[q._store._safe(q._leaf)].count_documents(
            q._mongo_filter(), session=(transaction._session if transaction else None)
        )
        return [[_AggResult(n)]]


class Query:
    def __init__(self, store: 'MongoFirestore', coll_path: str, *, group: bool = False):
        self._store = store
        self._coll_path = coll_path
        self._leaf = coll_path.split('/')[-1]
        self._group = group
        self._filters: List[dict] = []
        self._order: List[Tuple[str, int]] = []
        self._limit: Optional[int] = None
        self._offset: int = 0
        self._projection: Optional[dict] = None
        self._cursor: Optional[Tuple[list, bool]] = None

    def _clone(self) -> 'Query':
        q = Query(self._store, self._coll_path, group=self._group)
        q._filters = list(self._filters)
        q._order = list(self._order)
        q._limit = self._limit
        q._offset = self._offset
        q._projection = self._projection
        q._cursor = self._cursor
        return q

    def where(self, field=None, op=None, value=None, *, filter=None) -> 'Query':
        q = self._clone()
        if filter is not None:
            q._filters.append(_filter_to_mongo(filter))
        else:
            q._filters.append(_clause_to_mongo(field, op, value))
        return q

    def order_by(self, field: str, direction: str = ASCENDING) -> 'Query':
        q = self._clone()
        desc = str(direction).upper().endswith('DESCENDING') or direction == DESCENDING
        q._order.append((field, DESCENDING if desc else ASCENDING))
        return q

    def limit(self, n: int) -> 'Query':
        q = self._clone()
        q._limit = n
        return q

    def offset(self, n: int) -> 'Query':
        q = self._clone()
        q._offset = n
        return q

    def select(self, field_paths) -> 'Query':
        q = self._clone()
        q._projection = {f: 1 for f in field_paths} if field_paths else {'_id': 1}
        return q

    def count(self) -> _CountQuery:
        return _CountQuery(self)

    def start_after(self, doc) -> 'Query':
        return self._with_cursor(doc, inclusive=False)

    def start_at(self, doc) -> 'Query':
        return self._with_cursor(doc, inclusive=True)

    def _with_cursor(self, doc, inclusive: bool) -> 'Query':
        snap = doc.to_dict() if hasattr(doc, 'to_dict') else doc
        vals = [snap.get(f) for f, _ in self._order]
        q = self._clone()
        q._cursor = (vals, inclusive)
        return q

    def _mongo_filter(self) -> dict:
        and_parts: List[dict] = []
        if not self._group:
            and_parts.append({'_p': self._coll_path})
        and_parts.extend(self._filters)
        if self._cursor and self._order:
            and_parts.append(self._cursor_filter())
        if not and_parts:
            return {}
        if len(and_parts) == 1:
            return and_parts[0]
        return {'$and': and_parts}

    def _cursor_filter(self) -> dict:
        """Lexicographic cursor over the order_by fields (multi-field correct)."""
        vals, inclusive = self._cursor
        ors = []
        for i, (field, direction) in enumerate(self._order):
            clause = {}
            for j in range(i):
                clause[self._order[j][0]] = vals[j]
            strict = '$gt' if direction == ASCENDING else '$lt'
            last = i == len(self._order) - 1
            opf = ('$gte' if direction == ASCENDING else '$lte') if (last and inclusive) else strict
            clause[field] = {opf: vals[i]}
            ors.append(clause)
        return {'$or': ors} if len(ors) > 1 else ors[0]

    def stream(self, transaction=None) -> Iterable[DocumentSnapshot]:
        s = transaction._session if transaction else None
        cur = self._store._db[self._store._safe(self._leaf)].find(self._mongo_filter(), self._projection, session=s)
        if self._order:
            cur = cur.sort(self._order)
        if self._offset:
            cur = cur.skip(self._offset)
        if self._limit is not None:
            cur = cur.limit(self._limit)
        for raw in cur:
            yield DocumentSnapshot(DocumentReference(self._store, raw['_id']), raw)

    def get(self, transaction=None) -> List[DocumentSnapshot]:
        return list(self.stream(transaction=transaction))


class CollectionReference(Query):
    def __init__(self, store: 'MongoFirestore', path: str):
        super().__init__(store, path)
        self.path = path
        self.id = path.split('/')[-1]

    def document(self, doc_id: Optional[str] = None) -> DocumentReference:
        if doc_id is None:
            doc_id = uuid.uuid4().hex
        return DocumentReference(self._store, f'{self.path}/{doc_id}')

    def add(self, data: dict, document_id: Optional[str] = None):
        ref = self.document(document_id)
        ref.set(data)
        return (_now(), ref)


class WriteBatch:
    def __init__(self, store: 'MongoFirestore'):
        self._store = store
        self._ops: list = []

    def set(self, ref, data, merge: bool = False):
        self._ops.append(('set', ref, data, merge))

    def update(self, ref, data):
        self._ops.append(('update', ref, data, None))

    def delete(self, ref):
        self._ops.append(('delete', ref, None, None))

    def commit(self):
        for kind, ref, data, merge in self._ops:
            if kind == 'set':
                ref.set(data, merge=merge)
            elif kind == 'update':
                ref.update(data)
            elif kind == 'delete':
                ref.delete()
        self._ops.clear()


class Transaction:
    """Buffers writes; reads go through the active pymongo session. Driven by
    the @transactional decorator which runs it inside session.with_transaction()."""

    def __init__(self, store: 'MongoFirestore'):
        self._store = store
        self._session = None
        self._writes: list = []

    # read API used inside transactional functions
    def get(self, ref: DocumentReference) -> DocumentSnapshot:
        return ref.get(transaction=self)

    # buffered write API
    def set(self, ref, data, merge: bool = False):
        self._writes.append(lambda: ref.set(data, merge=merge, transaction=self))

    def update(self, ref, data):
        self._writes.append(lambda: ref.update(data, transaction=self))

    def delete(self, ref):
        self._writes.append(lambda: ref.delete(transaction=self))

    def _flush(self):
        for op in self._writes:
            op()
        self._writes.clear()


def transactional(fn):
    """Mirror google.cloud.firestore_v1.transactional: wrap fn(transaction, *a)."""

    @functools.wraps(fn)
    def wrapper(transaction: Transaction, *args, **kwargs):
        store = transaction._store
        result_box = {}

        def cb(session):
            transaction._session = session
            transaction._writes.clear()
            result_box['r'] = fn(transaction, *args, **kwargs)
            transaction._flush()
            return result_box['r']

        try:
            with store._client.start_session() as session:
                session.with_transaction(cb)
            return result_box['r']
        except OperationFailure:
            # Standalone Mongo (no replica set): degrade to non-atomic apply.
            transaction._session = None
            transaction._writes.clear()
            result_box['r'] = fn(transaction, *args, **kwargs)
            transaction._flush()
            return result_box['r']

    return wrapper


class MongoFirestore:
    """Drop-in replacement for firestore.Client(), backed by MongoDB."""

    def __init__(self, mongo_url: str, db_name: str):
        # tz_aware=True: pymongo returns naive UTC datetimes by default, but Firestore
        # returns tz-aware ones. App code does `datetime.now(timezone.utc) - stored_dt`,
        # which raises "can't subtract offset-naive and offset-aware datetimes" on naive
        # values. Returning tz-aware (UTC) datetimes makes the shim match Firestore.
        self._client = MongoClient(mongo_url, tz_aware=True)
        self._db = self._client[db_name]

    @staticmethod
    def _safe(name: str) -> str:
        return name.replace('$', '_').replace(' ', '_')

    def collection(self, path: str) -> CollectionReference:
        return CollectionReference(self, path)

    def collection_group(self, name: str) -> Query:
        return Query(self, name, group=True)

    def document(self, path: str) -> DocumentReference:
        return DocumentReference(self, path)

    def batch(self) -> WriteBatch:
        return WriteBatch(self)

    def transaction(self, **kwargs) -> Transaction:
        return Transaction(self)

    def get_all(self, references: Iterable[DocumentReference], **kwargs):
        for ref in references:
            yield ref.get()
