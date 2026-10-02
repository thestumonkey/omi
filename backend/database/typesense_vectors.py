"""Pinecone-Index stand-in backed by the cluster's Typesense. [fork-only]

``database/vector_db.py`` talks to one Pinecone ``Index`` object through five
calls: upsert, query, update, delete and list, with Pinecone's metadata filter
language. This class answers the same calls from a Typesense collection, so
the upstream vector code stays unchanged; the tail block of vector_db.py
swaps it in when SELF_HOSTED_VECTOR_STORE=typesense.

Storage: one collection (SELF_HOSTED_VECTOR_COLLECTION, default "vectors").
Each record is one document:

    id       "<namespace>:<pinecone id>"   (Typesense ids are collection-wide)
    _ns      Pinecone namespace
    _vid     Pinecone id
    _vec     the vector (cosine distance; width SELF_HOSTED_EMBED_DIM, default 768)
    _fields  names of the metadata keys present, so {"$exists": ...} filters work
    ...      metadata keys as top-level fields (types auto-detected)

Scores are cosine similarity (1 - Typesense's cosine distance), as Pinecone's.
Settings: TYPESENSE_HOST, TYPESENSE_HOST_PORT, TYPESENSE_PROTOCOL, TYPESENSE_API_KEY.
"""

import logging
import os
import threading
from typing import Any, Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)

_DIM = int(os.environ.get('SELF_HOSTED_EMBED_DIM', '768') or '768')
_COLLECTION = os.environ.get('SELF_HOSTED_VECTOR_COLLECTION', '').strip() or 'vectors'
_PER_PAGE = 250  # Typesense's page-size ceiling
_DELETE_BATCH = 100
_RESERVED = ('id', '_ns', '_vid', '_vec', '_fields')
_META_ID = '_m_id'  # metadata key "id" collides with the document id

# Filter constants: every document has a non-empty _fields list without this value.
_NEVER = '__never__'
FALSE = f'_fields:={_NEVER}'
TRUE = f'_fields:!={_NEVER}'


class UnsupportedFilter(ValueError):
    pass


# ── Pinecone filter → Typesense filter_by ────────────────────────────────────


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    # Backticks quote strings in filter_by and cannot be escaped inside one.
    return '`' + str(value).replace('`', '') + '`'


def _field(name: str) -> str:
    return _META_ID if name == 'id' else name


def _clause(name: str, op: str, value: Any, known: Optional[set]) -> str:
    if op == '$exists':
        return f'_fields:={_literal(name)}' if value else f'_fields:!={_literal(name)}'
    field = _field(name)
    if known is not None and field not in known:
        # No stored record has this field: positive tests cannot match, negative ones always do.
        return TRUE if op in ('$ne', '$nin') else FALSE
    if op == '$eq':
        return f'{field}:={_literal(value)}'
    if op == '$ne':
        return f'{field}:!={_literal(value)}'
    if op in ('$in', '$nin'):
        values = list(value or [])
        if not values:
            return FALSE if op == '$in' else TRUE
        joined = ','.join(_literal(v) for v in values)
        return f'{field}:=[{joined}]' if op == '$in' else f'{field}:!=[{joined}]'
    comparisons = {'$gt': '>', '$gte': '>=', '$lt': '<', '$lte': '<='}
    if op in comparisons:
        return f'{field}:{comparisons[op]}{_literal(value)}'
    raise UnsupportedFilter(f'unsupported Pinecone filter operator: {op}')


def _join(parts: List[str], glue: str) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ''
    return parts[0] if len(parts) == 1 else '(' + f' {glue} '.join(f'({p})' for p in parts) + ')'


def translate_filter(pinecone_filter: Optional[Dict[str, Any]], known: Optional[set] = None) -> str:
    """Pinecone metadata filter → Typesense filter_by. "" means no filter.

    `known` is the set of field names the collection has; clauses on other
    fields become constants (Typesense rejects filters on unknown fields).
    """
    if not pinecone_filter:
        return ''
    parts: List[str] = []
    for key, value in pinecone_filter.items():
        if key == '$and':
            parts.append(_join([translate_filter(sub, known) or TRUE for sub in value], '&&'))
        elif key == '$or':
            parts.append(_join([translate_filter(sub, known) or TRUE for sub in value], '||'))
        elif isinstance(value, dict):
            parts.append(_join([_clause(key, op, arg, known) for op, arg in value.items()], '&&'))
        else:  # {"field": value} is shorthand for $eq
            parts.append(_clause(key, '$eq', value, known))
    return _join(parts, '&&')


# ── Records ──────────────────────────────────────────────────────────────────


def to_document(namespace: str, record: Dict[str, Any]) -> Dict[str, Any]:
    metadata = {k: v for k, v in (record.get('metadata') or {}).items() if v is not None}
    doc: Dict[str, Any] = {_field(k): v for k, v in metadata.items() if k not in _RESERVED[1:]}
    doc.update(
        {
            'id': f"{namespace}:{record['id']}",
            '_ns': namespace,
            '_vid': record['id'],
            '_vec': [float(x) for x in record['values']],
            '_fields': sorted(metadata) or [_NEVER + '_empty'],
        }
    )
    return doc


def to_match(hit: Dict[str, Any], include_values: bool, include_metadata: bool) -> Dict[str, Any]:
    doc = hit['document']
    match: Dict[str, Any] = {'id': doc['_vid'], 'score': 1.0 - float(hit.get('vector_distance', 1.0))}
    if include_metadata:
        match['metadata'] = {('id' if k == _META_ID else k): v for k, v in doc.items() if k not in _RESERVED}
    if include_values:
        match['values'] = doc.get('_vec', [])
    return match


class TypesenseVectorIndex:
    """The subset of ``pinecone.Index`` that database/vector_db.py uses."""

    def __init__(self, client: Any = None, collection: str = _COLLECTION, dim: int = _DIM):
        self._client = client
        self._name = collection
        self._dim = dim
        self._lock = threading.Lock()
        self._ready = False
        self._known: Optional[set] = None

    # -- plumbing --

    def _ts(self):
        if self._client is None:
            import typesense

            self._client = typesense.Client(
                {
                    'nodes': [
                        {
                            'host': os.environ['TYPESENSE_HOST'],
                            'port': os.environ.get('TYPESENSE_HOST_PORT', '80'),
                            'protocol': os.environ.get('TYPESENSE_PROTOCOL', 'http'),
                        }
                    ],
                    'api_key': os.environ['TYPESENSE_API_KEY'],
                    'connection_timeout_seconds': 10,
                }
            )
        return self._client

    def _collection(self):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self._ensure_collection()
                    self._ready = True
        return self._ts().collections[self._name]

    def _ensure_collection(self) -> None:
        import typesense

        try:
            self._ts().collections[self._name].retrieve()
            return
        except typesense.exceptions.ObjectNotFound:
            pass
        self._ts().collections.create(
            {
                'name': self._name,
                'fields': [
                    {'name': '_ns', 'type': 'string', 'facet': True},
                    {'name': '_vid', 'type': 'string'},
                    {'name': '_fields', 'type': 'string[]'},
                    {'name': '_vec', 'type': 'float[]', 'num_dim': self._dim, 'vec_dist': 'cosine'},
                    {'name': '.*', 'type': 'auto', 'optional': True},
                ],
            }
        )
        logger.info('typesense vectors: created collection %s (dim %d)', self._name, self._dim)

    def _known_fields(self, refresh: bool = False) -> set:
        if self._known is None or refresh:
            schema = self._collection().retrieve()
            self._known = {f['name'] for f in schema.get('fields', [])}
        return self._known

    def _filter(self, namespace: str, pinecone_filter: Optional[Dict[str, Any]]) -> str:
        known = self._known_fields()
        names = _filter_field_names(pinecone_filter)
        if any(_field(n) not in known for n in names):
            known = self._known_fields(refresh=True)
        user_filter = translate_filter(pinecone_filter, known)
        return _join([f'_ns:={_literal(namespace)}', user_filter], '&&')

    def _width_ok(self, vector: List[float], what: str) -> bool:
        if len(vector) == self._dim:
            return True
        logger.warning('typesense vectors: %s has %d dims, collection has %d; skipped', what, len(vector), self._dim)
        return False

    # -- Pinecone API --

    def upsert(self, vectors: Iterable[Dict[str, Any]], namespace: str = '') -> Dict[str, int]:
        docs = [to_document(namespace, v) for v in vectors if self._width_ok(v['values'], f"vector {v['id']}")]
        if not docs:
            return {'upserted_count': 0}
        results = self._collection().documents.import_(docs, {'action': 'upsert', 'dirty_values': 'coerce_or_drop'})
        failed = [r for r in results if not r.get('success')]
        if failed:
            raise RuntimeError(f'typesense vectors: {len(failed)} of {len(docs)} upserts failed: {failed[0]}')
        self._known = None  # new metadata keys may have added fields
        return {'upserted_count': len(docs)}

    def query(
        self,
        vector: List[float],
        top_k: int = 10,
        filter: Optional[Dict[str, Any]] = None,  # noqa: A002 - Pinecone's keyword
        namespace: str = '',
        include_values: bool = False,
        include_metadata: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        if not self._width_ok(vector, 'query vector'):
            return {'matches': [], 'namespace': namespace}
        vec = ','.join(repr(float(x)) for x in vector)
        base = {
            'collection': self._name,
            'q': '*',
            'vector_query': f'_vec:([{vec}], k:{int(top_k)})',
            'filter_by': self._filter(namespace, filter),
            'exclude_fields': '' if include_values else '_vec',
        }
        matches: List[Dict[str, Any]] = []
        page = 1
        while len(matches) < top_k:
            per_page = min(_PER_PAGE, top_k - len(matches))
            search = {**base, 'per_page': per_page, 'page': page}
            result = self._ts().multi_search.perform({'searches': [search]}, {})['results'][0]
            if 'error' in result:
                raise RuntimeError(f"typesense vectors: query failed: {result['error']}")
            hits = result.get('hits', [])
            matches.extend(to_match(h, include_values, include_metadata) for h in hits)
            if len(hits) < per_page:
                break
            page += 1
        return {'matches': matches[:top_k], 'namespace': namespace}

    def update(
        self, id: str, set_metadata: Optional[Dict[str, Any]] = None, namespace: str = '', **_: Any
    ):  # noqa: A002
        import typesense

        try:
            doc = self._collection().documents[f'{namespace}:{id}'].retrieve()
        except typesense.exceptions.ObjectNotFound:
            return {}
        metadata = {('id' if k == _META_ID else k): v for k, v in doc.items() if k not in _RESERVED}
        metadata.update(set_metadata or {})
        self.upsert([{'id': id, 'values': doc['_vec'], 'metadata': metadata}], namespace=namespace)
        return {}

    def delete(
        self,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,  # noqa: A002 - Pinecone's keyword
        namespace: str = '',
        delete_all: bool = False,
        **_: Any,
    ) -> Dict[str, Any]:
        documents = self._collection().documents
        if ids:
            for i in range(0, len(ids), _DELETE_BATCH):
                joined = ','.join(_literal(v) for v in ids[i : i + _DELETE_BATCH])
                documents.delete({'filter_by': f'_ns:={_literal(namespace)} && _vid:=[{joined}]'})
        elif filter or delete_all:
            documents.delete({'filter_by': self._filter(namespace, None if delete_all else filter)})
        return {}

    def list(self, prefix: str = '', namespace: str = '', **_: Any) -> Iterator[List[str]]:
        exported = self._collection().documents.export(
            {'filter_by': f'_ns:={_literal(namespace)}', 'include_fields': '_vid'}
        )
        import json

        ids = [json.loads(line)['_vid'] for line in exported.splitlines() if line.strip()]
        matching = [i for i in ids if i.startswith(prefix)]
        if matching:
            yield matching

    def describe_index_stats(self, **_: Any) -> Dict[str, Any]:
        result = self._ts().multi_search.perform(
            {'searches': [{'collection': self._name, 'q': '*', 'facet_by': '_ns', 'per_page': 0}]}, {}
        )['results'][0]
        counts = {c['value']: {'vector_count': c['count']} for f in result.get('facet_counts', []) for c in f['counts']}
        return {'dimension': self._dim, 'namespaces': counts, 'total_vector_count': result.get('found', 0)}


def _filter_field_names(pinecone_filter: Optional[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for key, value in (pinecone_filter or {}).items():
        if key in ('$and', '$or'):
            for sub in value:
                names.extend(_filter_field_names(sub))
        elif not (isinstance(value, dict) and set(value) == {'$exists'}):
            names.append(key)
    return names
