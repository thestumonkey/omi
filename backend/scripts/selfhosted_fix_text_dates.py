"""Turn text timestamps left by the old Rust desktop backend into real dates. [fork-only]

The retired Rust desktop backend wrote datetimes as naive ISO text
("2026-06-25T10:17:51.049000"). Upstream code stores real dates in these
fields, so text values break date filters and sorts (Mongo orders strings and
dates as different types), Typesense indexing, and client decoding.

Only fields upstream writes as datetimes are touched. Collections that store
ISO text on purpose (memory_import_*, screen_activity) are left alone.
Text without a timezone is read as UTC.

    python scripts/selfhosted_fix_text_dates.py            # dry run: counts only
    python scripts/selfhosted_fix_text_dates.py --apply    # write

Needs MONGODB_URL (and MONGODB_DB, default "omi"). Safe to run again.
"""

import argparse
import os
import re
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$')

# collection -> field paths; "[]" steps into every element of a list.
TARGETS = {
    'conversations': [
        'created_at',
        'started_at',
        'finished_at',
        'audio_files[].started_at',
        'structured.action_items[].created_at',
        'structured.action_items[].updated_at',
        'structured.action_items[].completed_at',
        'structured.action_items[].due_at',
        'structured.events[].start',
    ],
    'memories': ['created_at', 'updated_at'],
    'messages': ['created_at'],
    'goals': ['created_at', 'updated_at'],
    'action_items': ['created_at', 'updated_at', 'completed_at', 'due_at'],
}


def parse_text_date(value):
    """The datetime for an ISO text timestamp, else None."""
    if not isinstance(value, str) or not _ISO.match(value):
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00').replace(' ', 'T', 1))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fix(node, steps):
    """Convert text dates at `steps` inside `node`, in place. Returns the count converted."""
    if not steps or not isinstance(node, dict):
        return 0
    key, rest = steps[0], steps[1:]
    in_list = key.endswith('[]')
    key = key[:-2] if in_list else key
    if key not in node:
        return 0
    if in_list:
        items = node[key] if isinstance(node[key], list) else []
        return sum(_fix(item, rest) for item in items)
    if rest:
        return _fix(node[key], rest)
    parsed = parse_text_date(node[key])
    if parsed is None:
        return 0
    node[key] = parsed
    return 1


def fix_document(doc, paths):
    """Returns ({top-level field: new value}, count) for the fields that changed."""
    changed, count = {}, 0
    for path in paths:
        n = _fix(doc, path.split('.'))
        if n:
            changed[path.split('.')[0].removesuffix('[]')] = None
            count += n
    return {field: doc[field] for field in changed}, count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='write the changes (default: dry run)')
    args = parser.parse_args()

    db = MongoClient(os.environ['MONGODB_URL'], tz_aware=True)[os.environ.get('MONGODB_DB', 'omi')]
    for name, paths in TARGETS.items():
        ops, fields = [], 0
        for doc in db[name].find({}):
            update, count = fix_document(doc, paths)
            if update:
                ops.append(UpdateOne({'_id': doc['_id']}, {'$set': update}))
                fields += count
        if args.apply and ops:
            db[name].bulk_write(ops, ordered=False)
        print(f'{name}: {len(ops)} documents, {fields} values {"fixed" if args.apply else "to fix"}')
    if not args.apply:
        print('Dry run. Add --apply to write.')


if __name__ == '__main__':
    main()
