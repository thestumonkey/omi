"""Pinecone-Index stand-in on Typesense. [fork-only]

The round-trip tests need a Typesense; set TYPESENSE_VECTORS_TEST_URL, e.g.
``http://localhost:18108`` after ``docker run -p 18108:8108 typesense/typesense:27.1
--data-dir /tmp --api-key=testkey``. Its API key is ``testkey``.
"""

import os
import uuid
from urllib.parse import urlparse

import pytest

from database.typesense_vectors import FALSE, TRUE, TypesenseVectorIndex, translate_filter


def test_filters_translate_to_filter_by():
    f = {
        '$and': [
            {'uid': {'$eq': 'u1'}},
            {'$or': [{'people': {'$in': ['Ann', 'Bo']}}, {'topics': {'$in': []}}]},
            {'created_at': {'$gte': 10, '$lte': 20}},
            {'memory_schema_version': {'$exists': False}},
        ]
    }
    assert translate_filter(f) == (
        '((uid:=`u1`) && (((people:=[`Ann`,`Bo`]) || (' + FALSE + '))) && '
        '(((created_at:>=10) && (created_at:<=20))) && (_fields:!=`memory_schema_version`))'
    )
    assert translate_filter({'uid': 'u1'}) == 'uid:=`u1`'
    assert translate_filter(None) == ''


def test_unknown_fields_become_constants():
    known = {'uid'}
    assert translate_filter({'ledger_kind': {'$in': ['a']}}, known) == FALSE
    assert translate_filter({'ledger_kind': {'$ne': 'a'}}, known) == TRUE
    assert translate_filter({'uid': {'$eq': 'u'}}, known) == 'uid:=`u`'


TEST_URL = os.environ.get('TYPESENSE_VECTORS_TEST_URL', '')
live = pytest.mark.skipif(not TEST_URL, reason='TYPESENSE_VECTORS_TEST_URL not set')


@pytest.fixture
def index():
    import typesense

    url = urlparse(TEST_URL)
    client = typesense.Client(
        {'nodes': [{'host': url.hostname, 'port': url.port, 'protocol': url.scheme}], 'api_key': 'testkey'}
    )
    name = f'vectors_test_{uuid.uuid4().hex[:8]}'
    yield TypesenseVectorIndex(client=client, collection=name, dim=3)
    client.collections[name].delete()


def _v(i, vec, **md):
    return {'id': i, 'values': vec, 'metadata': md}


@live
def test_round_trip(index):
    index.upsert(
        [
            _v('u1-a', [1, 0, 0], uid='u1', created_at=100.0, people=['Ann']),
            _v('u1-b', [0.9, 0.1, 0], uid='u1', created_at=200.0, people=['Bo'], memory_schema_version='v2'),
            _v('u2-c', [1, 0, 0], uid='u2', created_at=100.0),
        ],
        namespace='ns1',
    )
    res = index.query(vector=[1, 0, 0], top_k=5, filter={'uid': {'$eq': 'u1'}}, namespace='ns1', include_metadata=True)
    assert [m['id'] for m in res['matches']] == ['u1-a', 'u1-b']
    assert res['matches'][0]['score'] == pytest.approx(1.0, abs=1e-4)
    assert res['matches'][0]['metadata']['people'] == ['Ann']

    legacy = index.query(
        vector=[1, 0, 0],
        top_k=5,
        filter={'$and': [{'uid': {'$eq': 'u1'}}, {'memory_schema_version': {'$exists': False}}]},
        namespace='ns1',
    )
    assert [m['id'] for m in legacy['matches']] == ['u1-a']
    assert (
        index.query(vector=[1, 0, 0], top_k=5, filter={'ledger_kind': {'$in': ['x']}}, namespace='ns1')['matches'] == []
    )

    index.update('u1-a', set_metadata={'people': ['Cy']}, namespace='ns1')
    again = index.query(vector=[1, 0, 0], top_k=1, filter={'uid': 'u1'}, namespace='ns1', include_metadata=True)
    assert again['matches'][0]['metadata']['people'] == ['Cy']

    assert list(index.list(prefix='u1-', namespace='ns1')) == [['u1-a', 'u1-b']] or sorted(
        next(index.list(prefix='u1-', namespace='ns1'))
    ) == ['u1-a', 'u1-b']
    index.delete(ids=['u1-a'], namespace='ns1')
    index.delete(filter={'uid': {'$eq': 'u2'}}, namespace='ns1')
    assert index.describe_index_stats()['namespaces'] == {'ns1': {'vector_count': 1}}
    assert index.query(vector=[1, 0], top_k=3, namespace='ns1') == {'matches': [], 'namespace': 'ns1'}
