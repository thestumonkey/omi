"""TypesenseVectorIndex covers what upstream's owner routing calls. [fork-only]

database/pinecone_partition_index.py wraps the vector index for each user. It
calls fetch() before updates and deletes, passes _return_http_data_only=False
to writes, and reads freshness numbers from the headers. When fetch() was
missing, every sync failed at finalize. Needs no Typesense.
"""

import inspect
import re

from database import pinecone_partition_index as routing
from database.typesense_vectors import TypesenseVectorIndex

SOURCE = inspect.getsource(routing)
CALLED = set(re.findall(r"self\.index\.(\w+)\(", SOURCE)) | set(
    re.findall(r"_(?:mutate|provider_mutation)\((?:self\.index, )?'(\w+)'", SOURCE)
)


def test_routing_calls_were_found():
    assert {'fetch', 'query', 'upsert', 'delete'} <= CALLED


def test_index_has_every_method_routing_calls():
    missing = sorted(m for m in CALLED if not callable(getattr(TypesenseVectorIndex, m, None)))
    assert missing == []


def test_writes_return_http_info_with_a_request_lsn():
    index = TypesenseVectorIndex(client=object(), dim=3)
    assert index.upsert([], namespace='ns1') == {'upserted_count': 0}
    body, status, headers = index.upsert([], namespace='ns1', _return_http_data_only=False)
    assert body == {'upserted_count': 0} and status == 200
    assert int(headers['x-pinecone-request-lsn']) > 0
    # routing reads the number this way
    assert routing._body_and_lsn((body, status, headers))[1] == int(headers['x-pinecone-request-lsn'])


def test_query_lsn_is_never_older_than_an_earlier_write():
    index = TypesenseVectorIndex(client=object(), dim=3)
    _, _, write = index.upsert([], namespace='ns1', _return_http_data_only=False)
    # a query vector of the wrong width returns early, with no network call
    result, _, read = index._query(namespace='ns1', vector=[1.0], top_k=1, _return_http_data_only=False)
    assert result['matches'] == []
    assert int(read['x-pinecone-max-indexed-lsn']) > int(write['x-pinecone-request-lsn'])
