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
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ConfigurationError, DuplicateKeyError, InvalidOperation, OperationFailure

# The one exception google's @firestore.transactional retries. Raising it from
# Transaction._commit lets upstream's retry semantics work over Mongo unchanged.
from google.api_core.exceptions import Aborted

from google.cloud import firestore as _fs
from google.cloud.firestore_v1.transforms import Increment, ArrayUnion, ArrayRemove
from google.cloud.firestore_v1.base_query import BaseCompositeFilter

DELETE_FIELD = _fs.DELETE_FIELD
SERVER_TIMESTAMP = _fs.SERVER_TIMESTAMP

_RESERVED = ('_id', '_p', '_k', '_ut')


# ── Write preconditions (Firestore's option= on update/delete) ──────────────
# Every write stores `_ut`, a revision number that is new for each write. A
# snapshot gives it back as `update_time`. A LastUpdateOption built from that
# snapshot becomes a filter on the Mongo write, so the write happens only if
# nobody wrote the document since the read: a compare-and-set. A precondition
# that the shim cannot check fails closed with FailedPrecondition.
_rev_lock = threading.Lock()
_last_rev = 0


def _next_rev() -> int:
    """A revision that is larger than any earlier one from this process."""
    global _last_rev
    with _rev_lock:
        _last_rev = max(time.time_ns(), _last_rev + 1)
        return _last_rev


class _UpdateTime(datetime):
    """A datetime for update_time that also holds the exact revision."""

    _rev: int

    @classmethod
    def from_rev(cls, rev: int) -> '_UpdateTime':
        t = _EPOCH + timedelta(microseconds=rev // 1000)  # integer math: no float rounding
        out = cls(t.year, t.month, t.day, t.hour, t.minute, t.second, t.microsecond, tzinfo=timezone.utc)
        out._rev = rev
        return out


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class _WriteResult:
    """Firestore's WriteResult: upstream reads result.update_time after a write."""

    def __init__(self, rev: int):
        self.update_time = _UpdateTime.from_rev(rev)


def _rev_filter(t) -> dict:
    """Mongo filter on `_ut` for a last_update_time value.

    The exact revision when it came from this store. A plain datetime (for
    example update_time copied into a conversation's updated_at and loaded back)
    matches at microsecond precision, like Firestore timestamps. Revisions are
    nanosecond-unique, so two writes to one document within one microsecond are
    the only case a stale value could match.
    """
    from google.api_core.exceptions import FailedPrecondition

    rev = getattr(t, '_rev', None)
    if isinstance(rev, int):
        return {'_ut': rev}
    if isinstance(t, datetime):
        if t.tzinfo is None:
            raise FailedPrecondition(f'update_time {t!r} has no time zone')
        us = (t - _EPOCH) // timedelta(microseconds=1)
    elif hasattr(t, 'seconds') and hasattr(t, 'nanos'):  # protobuf Timestamp
        us = int(t.seconds) * 1_000_000 + int(t.nanos) // 1000
    else:
        raise FailedPrecondition(f'unsupported update_time {type(t).__name__}')
    return {'_ut': {'$gte': us * 1000, '$lt': (us + 1) * 1000}}


def _precondition(option) -> Tuple[dict, Optional[bool]]:
    """Return (extra Mongo filter, required existence) for a write option."""
    from google.api_core.exceptions import FailedPrecondition

    if option is None:
        return {}, None
    if hasattr(option, '_last_update_time'):
        t = option._last_update_time
        if t is None:
            # The read saw a document written before revisions existed.
            return {'_ut': {'$exists': False}}, True
        return _rev_filter(t), True
    if hasattr(option, '_exists'):
        return {}, bool(option._exists)
    raise FailedPrecondition(f'unsupported write option {type(option).__name__}')


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


# Firestore's name for the document id in filters, order_by and cursors
# (also what FieldPath.document_id() renders as). Stored here as the full path in _id.
_DOC_ID = '__name__'


def _mongo_field(field: str) -> str:
    return '_id' if field == _DOC_ID else field


def _filter_to_mongo(f, doc_key=lambda v: v) -> dict:
    """Translate a FieldFilter or BaseCompositeFilter into a Mongo expression."""
    if isinstance(f, BaseCompositeFilter):
        op = getattr(f, 'operator', 'AND')
        op = str(op).upper().replace('OPERATOR.', '')
        sub = [_filter_to_mongo(x, doc_key) for x in f.filters]
        return {'$or': sub} if 'OR' in op else {'$and': sub}
    # FieldFilter
    field = str(f.field_path)
    value = doc_key(f.value) if field == _DOC_ID else f.value
    return _clause_to_mongo(_mongo_field(field), f.op_string, value)


def _is_sentinel(val) -> bool:
    return isinstance(val, (Increment, ArrayUnion, ArrayRemove)) or val is DELETE_FIELD or val is SERVER_TIMESTAMP


def _sentinel_op(val) -> Tuple[str, Any]:
    if isinstance(val, Increment):
        return '$inc', val.value
    if isinstance(val, ArrayUnion):
        return '$addToSet', {'$each': list(val.values)}
    if isinstance(val, ArrayRemove):
        return '$pull', {'$in': list(val.values)}
    if val is DELETE_FIELD:
        return '$unset', ''
    return '$set', _now()  # SERVER_TIMESTAMP


def _split_transforms(data: Dict[str, Any], merge: bool = False) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """Expand Firestore sentinels into Mongo update specs: `(spec, later)`.

    `merge=True` (set(..., merge=True)) deep-merges like Firestore: every leaf
    becomes its own dotted path, so `{'a': {'b': Increment(1)}}` is `$inc a.b`
    and sibling keys already stored under `a` survive. Everything is in `spec`.

    Otherwise a map value replaces the whole field. Sentinels nested inside such
    a map cannot share one Mongo update with the `$set` of their parent (Mongo
    rejects the path conflict), so they come back in `later`, to apply after.
    """
    spec: Dict[str, Dict] = {}
    later: Dict[str, Dict] = {}

    def put(target, op, field, val):
        target.setdefault(op, {})[field] = val

    def flatten(prefix, obj):
        for key, val in obj.items():
            path = f'{prefix}.{key}' if prefix else key
            if _is_sentinel(val):
                op, arg = _sentinel_op(val)
                put(spec, op, path, arg)
            elif isinstance(val, dict) and val:
                flatten(path, val)
            else:
                put(spec, '$set', path, val)

    def strip(prefix, obj):
        # Plain copy of a map for `$set`; nested sentinels move to `later`.
        out = {}
        for key, val in obj.items():
            path = f'{prefix}.{key}'
            if val is SERVER_TIMESTAMP:
                out[key] = _now()
            elif _is_sentinel(val):
                op, arg = _sentinel_op(val)
                put(later, op, path, arg)
            elif isinstance(val, dict):
                out[key] = strip(path, val)
            else:
                out[key] = val
        return out

    if merge:
        flatten('', data)
        return spec, later
    for field, val in data.items():
        if _is_sentinel(val):
            op, arg = _sentinel_op(val)
            put(spec, op, field, arg)
        elif isinstance(val, dict):
            put(spec, '$set', field, strip(field, val))
        else:
            put(spec, '$set', field, val)
    return spec, later


_MISSING = object()


def _dotted_parents(*specs) -> set:
    """Every proper prefix of a dotted field path in these update specs."""
    parents = set()
    for spec in specs:
        for op, fields in (spec or {}).items():
            if not op.startswith('$') or not isinstance(fields, dict):
                continue
            for key in fields:
                parts = key.split('.')
                parents.update('.'.join(parts[:i]) for i in range(1, len(parts)))
    return parents


def _value_at(doc: dict, path: str):
    cur = doc
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


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
        rev = raw.get('_ut') if raw else None
        self.update_time = _UpdateTime.from_rev(rev) if isinstance(rev, int) else None

    def to_dict(self) -> Optional[dict]:
        if self._raw is None:
            return None
        return {k: v for k, v in self._raw.items() if k not in _RESERVED}

    def get(self, field: str):
        return (self.to_dict() or {}).get(field)


# Firestore read/write methods take retry= and timeout=. Upstream passes them
# often, for example a 3-second bounded read. The shim accepts and ignores
# them: pymongo uses its own timeouts. update/delete also take option=, a write
# precondition, which the shim enforces (see _precondition).
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

    def collections(self) -> Iterable['CollectionReference']:
        """Every subcollection that actually holds a document under this document.

        Needed by `_client.delete_collection_recursive` — the cascade that stops a
        deleted user or conversation leaving unreachable orphans behind. Firestore
        enumerates subcollections natively; here they are discovered from stored
        paths: a child document carries `_p` = its parent collection path, so this
        document's subcollections are the distinct `_p` values one segment below
        `self.path`.

        Documents live in the Mongo collection named for their *leaf* collection, so
        the children are spread across collections and a single query cannot find
        them — hence the scan. Deletes are rare and already batched, and the `_p`
        index keeps each lookup cheap; the alternative (a per-parent subcollection
        registry) would add a write to every set() and can drift whenever anything
        touches Mongo outside this shim.
        """
        pattern = f'^{re.escape(self.path)}/[^/]+$'
        seen = set()
        for mongo_name in self._store._db.list_collection_names():
            for parent_path in self._store._db[mongo_name].distinct('_p', {'_p': {'$regex': pattern}}):
                if parent_path not in seen:
                    seen.add(parent_path)
                    yield CollectionReference(self._store, parent_path)

    def _session(self, transaction):
        return transaction._session if transaction is not None else None

    def _replace_non_map_parents(self, specs, session, extra_filter=None):
        """Firestore writes ``a.b`` even when ``a`` is null or a scalar: ``a``
        becomes a map. Mongo refuses ("Cannot create field 'b' in element
        {a: null}"), so turn such parents into empty maps first."""
        parents = _dotted_parents(*specs)
        if not parents:
            return
        top = {p.split('.')[0]: 1 for p in parents}
        raw = self._mc().find_one({'_id': self.path, **(extra_filter or {})}, top, session=session)
        if not raw:
            return
        fix = {}
        for path in sorted(parents, key=lambda p: p.count('.')):
            if any(path.startswith(f + '.') for f in fix):
                continue
            value = _value_at(raw, path)
            if value is not _MISSING and not isinstance(value, dict):
                fix[path] = {}
        if fix:
            self._mc().update_one({'_id': self.path, **(extra_filter or {})}, {'$set': fix}, session=session)

    def set(self, data: dict, merge: bool = False, transaction=None, retry=None, timeout=None):
        spec, later = _split_transforms(data, merge=merge)
        base = {'_id': self.path, '_p': self._parent, '_k': self.id, '_ut': _next_rev()}
        s = self._session(transaction)
        if merge:
            spec.setdefault('$set', {}).update(base)
            self._replace_non_map_parents((spec, later), s)
            self._mc().update_one({'_id': self.path}, spec, upsert=True, session=s)
        else:
            doc = dict(base)
            doc.update(spec.pop('$set', {}))
            self._mc().replace_one({'_id': self.path}, doc, upsert=True, session=s)
            # Transforms (Increment, ArrayUnion, ...) apply on top of the new document.
            for extra in (spec, later):
                if extra:
                    self._mc().update_one({'_id': self.path}, extra, session=s)
        return _WriteResult(base['_ut'])

    def create(self, data: dict, transaction=None, retry=None, timeout=None):
        """Write a new document; fail with Conflict (409) if it already exists, like Firestore."""
        spec, later = _split_transforms(data)
        doc = {'_id': self.path, '_p': self._parent, '_k': self.id, '_ut': _next_rev()}
        doc.update(spec.pop('$set', {}))
        s = self._session(transaction)
        try:
            self._mc().insert_one(doc, session=s)
        except DuplicateKeyError as e:
            from google.api_core.exceptions import Conflict

            raise Conflict(f'Document already exists: {self.path}') from e
        # Transforms (Increment, ArrayUnion, ...) apply on top of the new document.
        for extra in (spec, later):
            if extra:
                self._mc().update_one({'_id': self.path}, extra, session=s)
        return _WriteResult(doc['_ut'])

    def update(self, data: dict, transaction=None, option=None, retry=None, timeout=None):
        spec, later = _split_transforms(data)
        extra_filter, must_exist = _precondition(option)
        if must_exist is False:
            from google.api_core.exceptions import FailedPrecondition

            raise FailedPrecondition(f'update needs an existing document: {self.path}')
        rev = _next_rev()
        spec.setdefault('$set', {}).update({'_p': self._parent, '_k': self.id, '_ut': rev})
        s = self._session(transaction)
        self._replace_non_map_parents((spec, later), s, extra_filter)
        res = self._mc().update_one({'_id': self.path, **extra_filter}, spec, upsert=False, session=s)
        if res.matched_count == 0:
            from google.api_core.exceptions import FailedPrecondition, NotFound

            if extra_filter:
                raise FailedPrecondition(f'document changed since it was read: {self.path}')
            raise NotFound(f'No document to update: {self.path}')
        if later:
            self._mc().update_one({'_id': self.path}, later, session=s)
        return _WriteResult(rev)

    def get(self, field_paths=None, transaction=None, retry=None, timeout=None) -> DocumentSnapshot:
        # Firestore's DocumentReference.get(field_paths=None, transaction=None):
        # the first positional arg is an optional list of field paths to project.
        projection = None
        if field_paths:
            # always keep the bookkeeping fields so DocumentSnapshot stays well-formed
            projection = {f: 1 for f in field_paths}
            projection.update({'_id': 1, '_p': 1, '_k': 1, '_ut': 1})
        raw = self._mc().find_one({'_id': self.path}, projection, session=self._session(transaction))
        return DocumentSnapshot(self, raw)

    def delete(self, transaction=None, option=None, retry=None, timeout=None):
        extra_filter, must_exist = _precondition(option)
        s = self._session(transaction)
        if must_exist is False:
            if self._mc().find_one({'_id': self.path}, {'_id': 1}, session=s) is not None:
                from google.api_core.exceptions import FailedPrecondition

                raise FailedPrecondition(f'delete needs a missing document: {self.path}')
            return _UpdateTime.from_rev(_next_rev())
        res = self._mc().delete_one({'_id': self.path, **extra_filter}, session=s)
        if must_exist and res.deleted_count == 0:
            from google.api_core.exceptions import FailedPrecondition

            raise FailedPrecondition(f'document changed or missing since it was read: {self.path}')
        return _UpdateTime.from_rev(_next_rev())  # Firestore returns the delete time


class _CountQuery:
    def __init__(self, query: 'Query'):
        self._query = query

    def get(self, transaction=None, retry=None, timeout=None):
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

    def _doc_key(self, value: Any) -> Any:
        """A document-id value (``__name__``) as the stored ``_id``: its full path."""
        if isinstance(value, DocumentReference):
            return value.path
        if isinstance(value, (list, tuple)):
            return [self._doc_key(v) for v in value]
        if isinstance(value, str) and '/' not in value and not self._group:
            return f'{self._coll_path}/{value}'
        return value

    def where(self, field=None, op=None, value=None, *, filter=None) -> 'Query':
        q = self._clone()
        if filter is not None:
            q._filters.append(_filter_to_mongo(filter, q._doc_key))
        else:
            field = str(field)
            if field == _DOC_ID:
                value = q._doc_key(value)
            q._filters.append(_clause_to_mongo(_mongo_field(field), op, value))
        return q

    def order_by(self, field: str, direction: str = ASCENDING) -> 'Query':
        q = self._clone()
        desc = str(direction).upper().endswith('DESCENDING') or direction == DESCENDING
        q._order.append((_mongo_field(str(field)), DESCENDING if desc else ASCENDING))
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
        # Keep _ut so a selected snapshot still has update_time for preconditions.
        q._projection = {**{f: 1 for f in field_paths}, '_ut': 1} if field_paths else {'_id': 1, '_ut': 1}
        return q

    def count(self) -> _CountQuery:
        return _CountQuery(self)

    def start_after(self, doc) -> 'Query':
        return self._with_cursor(doc, inclusive=False)

    def start_at(self, doc) -> 'Query':
        return self._with_cursor(doc, inclusive=True)

    def _with_cursor(self, doc, inclusive: bool) -> 'Query':
        snap = (doc.to_dict() or {}) if hasattr(doc, 'to_dict') else dict(doc)
        if hasattr(doc, 'reference'):  # a snapshot: its own path is the document id
            snap['_id'] = doc.reference.path
        elif _DOC_ID in snap:
            snap['_id'] = self._doc_key(snap.pop(_DOC_ID))
        vals = [snap.get(f) for f, _ in self._order]
        q = self._clone()
        q._cursor = (vals, inclusive)
        return q

    def _mongo_filter(self) -> dict:
        and_parts: List[dict] = []
        if not self._group:
            and_parts.append({'_p': self._coll_path})
        else:
            # Only documents this shim wrote. A collection can also hold rows in
            # another layout (e.g. the pre-shim native rewrite, keyed by bare id),
            # which have no path and are not Firestore documents.
            and_parts.append({'_p': {'$exists': True}})
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

    def stream(self, transaction=None, retry=None, timeout=None) -> Iterable[DocumentSnapshot]:
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

    def get(self, transaction=None, retry=None, timeout=None) -> List[DocumentSnapshot]:
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

    def add(self, data: dict, document_id: Optional[str] = None, retry=None, timeout=None):
        ref = self.document(document_id)
        ref.set(data)
        return (_now(), ref)


class WriteBatch:
    def __init__(self, store: 'MongoFirestore'):
        self._store = store
        self._ops: list = []

    def set(self, ref, data, merge: bool = False):
        self._ops.append(('set', ref, data, merge))

    def create(self, ref, data):
        self._ops.append(('create', ref, data, None))

    def update(self, ref, data, option=None):
        self._ops.append(('update', ref, data, option))

    def delete(self, ref, option=None):
        self._ops.append(('delete', ref, None, option))

    def commit(self, retry=None, timeout=None):
        # The 4th slot is merge for set, and the write option for update/delete.
        results = []
        for kind, ref, data, extra in self._ops:
            if kind == 'set':
                results.append(ref.set(data, merge=extra))
            elif kind == 'create':
                results.append(ref.create(data))
            elif kind == 'update':
                results.append(ref.update(data, option=extra))
            elif kind == 'delete':
                ref.delete(option=extra)
                results.append(_WriteResult(_next_rev()))
        self._ops.clear()
        return results


class Transaction:
    """Buffers writes; reads go through the active pymongo session.

    Implements the private protocol that google's own ``@firestore.transactional``
    decorator drives (``_clean_up`` / ``_begin`` / ``_commit`` / ``_rollback`` plus
    ``_id`` / ``_read_only`` / ``_max_attempts``). That matters because upstream uses
    ``@firestore.transactional`` — google's real decorator — in ~20 database modules
    and ~90 call sites. Supporting the protocol here means every one of those runs
    unchanged, instead of each file needing its import repointed at our own
    ``transactional`` below.

    Retries: google's decorator retries ``google.api_core.exceptions.Aborted``, so
    ``_commit`` translates Mongo's transient/write-conflict failures into it.
    """

    _read_only = False

    def __init__(self, store: 'MongoFirestore', max_attempts: int = 5):
        self._store = store
        self._session = None
        self._writes: list = []
        self._max_attempts = max_attempts
        self._id = None
        self._in_txn = False

    # read API used inside transactional functions
    def get(self, ref: DocumentReference, retry=None, timeout=None) -> DocumentSnapshot:
        return ref.get(transaction=self)

    # buffered write API
    def set(self, ref, data, merge: bool = False):
        self._writes.append(lambda: ref.set(data, merge=merge, transaction=self))

    def create(self, ref, data):
        self._writes.append(lambda: ref.create(data, transaction=self))

    def update(self, ref, data, option=None):
        self._writes.append(lambda: ref.update(data, transaction=self, option=option))

    def delete(self, ref, option=None):
        self._writes.append(lambda: ref.delete(transaction=self, option=option))

    def _flush(self):
        for op in self._writes:
            op()
        self._writes.clear()

    # --- protocol driven by google.cloud.firestore_v1.transaction._Transactional ---

    def _end_session(self):
        if self._session is not None:
            try:
                self._session.end_session()
            except Exception:
                pass
        self._session = None
        self._in_txn = False

    def _clean_up(self):
        self._writes.clear()
        self._end_session()
        self._id = None

    def _begin(self, retry_id=None):
        self._writes.clear()
        self._session = self._store._client.start_session()
        try:
            self._session.start_transaction()
            self._in_txn = True
        except (OperationFailure, ConfigurationError, InvalidOperation):
            # Standalone Mongo (no replica set) cannot do multi-document transactions.
            # Degrade to a non-atomic apply rather than failing the caller outright —
            # same fallback the module-level `transactional` below has always taken.
            self._in_txn = False
        self._id = retry_id or uuid.uuid4().hex

    def _commit(self):
        self._flush()
        if self._in_txn:
            try:
                self._session.commit_transaction()
            except OperationFailure as exc:
                labels = (
                    getattr(exc, '_error_labels', None) or exc.details.get('errorLabels', []) if exc.details else []
                )
                self._end_session()
                # Mongo's retryable transaction failures map onto the one exception
                # google's decorator retries.
                if 'TransientTransactionError' in labels or 'UnknownTransactionCommitResult' in labels:
                    raise Aborted(str(exc)) from exc
                raise
        self._end_session()
        return []

    def _rollback(self):
        self._writes.clear()
        if self._in_txn:
            try:
                self._session.abort_transaction()
            except Exception:
                pass
        self._end_session()


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

    @staticmethod
    def write_option(**kwargs):
        """Firestore's write_option(last_update_time=...|exists=...); see _precondition."""
        from google.cloud.firestore_v1.base_client import BaseClient

        return BaseClient.write_option(**kwargs)

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
