"""Move memory-search vectors from Pinecone to the cluster's Typesense. [fork-only]

The new embedding model makes different vectors, so every record is rebuilt
with the local embedding server (SELF_HOSTED_EMBED_URL):

    ns1  conversations   rebuilt from MongoDB by upstream's save_structured_vector
                         (one local-LLM call each for people/topics/entities),
                         for every completed, non-discarded conversation
    ns2  memories        labels from Pinecone, text = memory.content
    ns4  tasks           labels from Pinecone, text = action_item.description

Pinecone records whose source document is gone are skipped (deleted items or
the old pre-shim data layout).

    python scripts/selfhosted_migrate_vectors.py           # dry run: counts only
    python scripts/selfhosted_migrate_vectors.py --apply   # embed + write

Needs PINECONE_API_KEY/PINECONE_INDEX_NAME (read only), MONGODB_URL,
SELF_HOSTED_EMBED_URL, SELF_HOSTED_VECTOR_STORE=typesense and TYPESENSE_*.
Safe to run again (upserts).
"""

import argparse
import os
import sys
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BATCH = 50


def _doc(db, uid: str, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    if not uid or not doc_id:
        return None
    snapshot = db.collection('users').document(uid).collection(collection).document(doc_id).get()
    data = snapshot.to_dict() if snapshot.exists else None
    if not data or data.get('deleted'):
        return None
    return data


def _memory_text(db, md: Dict[str, Any]) -> Optional[str]:
    data = _doc(db, md.get('uid'), 'memories', md.get('memory_id'))
    return (data or {}).get('content') or None


def _task_text(db, md: Dict[str, Any]) -> Optional[str]:
    data = _doc(db, md.get('uid'), 'action_items', md.get('action_item_id'))
    return (data or {}).get('description') or None


SOURCES: Dict[str, Callable[[Any, Dict[str, Any]], Optional[str]]] = {
    'ns2': _memory_text,
    'ns4': _task_text,
}


def _pinecone_records(index, namespace: str):
    for page in index.list(namespace=namespace, limit=100):
        fetched = index.fetch(ids=list(page), namespace=namespace)
        for vid, vector in fetched.vectors.items():
            yield vid, dict(vector.metadata or {})


def rebuild_conversations(db, apply: bool) -> None:
    import database.conversations as conversations_db
    from models.conversation import Conversation
    from utils.conversations.process_conversation import save_structured_vector

    done = skipped = failed = 0
    for snapshot in db.collection_group('conversations').stream():
        data = snapshot.to_dict() or {}
        parts = snapshot.reference.path.split('/')
        if len(parts) != 4 or parts[0] != 'users':
            continue
        if data.get('deleted') or data.get('discarded') or data.get('status') != 'completed':
            skipped += 1
            continue
        if not (data.get('structured') or {}).get('title'):
            skipped += 1
            continue
        if not apply:
            done += 1
            continue
        try:
            # get_conversation decrypts transcript fields stored encrypted at rest.
            full = conversations_db.get_conversation(parts[1], parts[3])
            save_structured_vector(parts[1], Conversation(**full))
            done += 1
        except Exception as e:  # one bad record must not stop the rest
            failed += 1
            print(f'  ns1 {parts[3]}: {type(e).__name__}: {e}'[:200])
    verb = 'rebuilt' if apply else 'to rebuild'
    print(f'ns1: {done} {verb}, {skipped} skipped (discarded/unfinished/untitled), {failed} failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='embed and write (default: dry run)')
    args = parser.parse_args()

    from pinecone import Pinecone

    from database._client import db
    from database.typesense_vectors import TypesenseVectorIndex
    from utils.llm.selfhosted import make_embeddings

    source = Pinecone(api_key=os.environ['PINECONE_API_KEY']).Index(os.environ['PINECONE_INDEX_NAME'])
    target = TypesenseVectorIndex()
    embeddings = make_embeddings()

    if os.environ.get('SELF_HOSTED_VECTOR_STORE', '').strip().lower() != 'typesense':
        sys.exit('Set SELF_HOSTED_VECTOR_STORE=typesense so conversation vectors go to Typesense.')
    rebuild_conversations(db, args.apply)

    for namespace, text_for in SOURCES.items():
        pending: List[Dict[str, Any]] = []
        moved = skipped = 0

        def flush():
            nonlocal moved
            if not pending:
                return
            vectors = embeddings.embed_documents([p['text'] for p in pending])
            target.upsert(
                [{'id': p['id'], 'values': v, 'metadata': p['metadata']} for p, v in zip(pending, vectors)],
                namespace=namespace,
            )
            moved += len(pending)
            pending.clear()

        for vid, metadata in _pinecone_records(source, namespace):
            text = text_for(db, metadata)
            if not text:
                skipped += 1
                continue
            if not args.apply:
                moved += 1
                continue
            pending.append({'id': vid, 'text': text, 'metadata': metadata})
            if len(pending) >= _BATCH:
                flush()
        if args.apply:
            flush()
        print(f'{namespace}: {moved} {"moved" if args.apply else "to move"}, {skipped} skipped (no source document)')
    if not args.apply:
        print('Dry run. Add --apply to write.')


if __name__ == '__main__':
    main()
