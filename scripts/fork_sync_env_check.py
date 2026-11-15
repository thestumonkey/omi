#!/usr/bin/env python3
"""[fork-only] List upstream server switches that changed between two refs.

Upstream's backend/deploy/runtime_env.yaml lists the settings each upstream
server runs with. A new switch there can be needed for the code to work, with
no merge conflict and no release note. Example: OMI_SHAPED_AGENT_MODE=on; with
it unset, every conversation summary failed after a sync.

This compares the file at OLD and NEW for the servers the fork runs (prod
backend-listen and pusher), and keeps only switches (categories rollout and
ops_kill). It drops a setting when the fork already sets the same value, or
when the code default (os.getenv(NAME, default)) is the same as upstream's
value, because then leaving it unset changes nothing.

The rest goes in two groups:
  switches  upstream turns something on, or picks a named mode. Read these.
  tuning    numbers and lists of upstream's own test users. Counted only;
            --all prints them.

  fork_sync_env_check.py [--all] OLD NEW

Always exits 0: the list is for a person to read, not a check that fails.
"""

import re
import subprocess
import sys

import yaml

RUNTIME_ENV = 'backend/deploy/runtime_env.yaml'
SERVICES = ('backend-listen', 'pusher')
CATEGORIES = ('rollout', 'ops_kill')
# The fork sets omi-backend's settings here. pusher and desktop-backend-py run
# the same image; their settings live in the cluster, not in this repo.
FORK_VALUES = 'backend/charts/backend-listen/local_omi_backend_listen_values.yaml'
_TRUE = {'1', 'true', 'yes', 'on'}
_FALSE = {'0', 'false', 'no', 'off', ''}
_USER_LIST = re.compile(r'_(UID_ALLOWLIST|UIDS|ALLOWLIST|COHORT)$')


def _show(ref: str, path: str):
    out = subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True, text=True)
    return yaml.safe_load(out.stdout) if out.returncode == 0 else None


def _same(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or (a in _TRUE and b in _TRUE) or (a in _FALSE and b in _FALSE)


def switches(doc) -> dict:
    """{name: (value, [services])} for the switches the fork's servers get."""
    result: dict = {}
    gke = ((doc or {}).get('environments') or {}).get('prod', {}).get('gke') or {}
    for service in SERVICES:
        for name, entry in ((gke.get(service) or {}).get('env') or {}).items():
            if not isinstance(entry, dict) or entry.get('category') not in CATEGORIES or 'value' not in entry:
                continue
            value, where = result.get(name, (str(entry['value']), []))
            result[name] = (value, where + [service])
    return result


def fork_values(doc) -> dict:
    return {
        e['name']: str(e['value'])
        for e in (doc or {}).get('env') or []
        if isinstance(e, dict) and 'name' in e and 'value' in e
    }


def code_default(ref: str, name: str):
    """The literal default in os.getenv/os.environ.get(NAME, default) at ref.

    None when the code reads the setting another way, or with defaults that
    disagree.
    """
    pattern = rf"""(getenv|environ\.get)\([[:space:]]*['"]{name}['"][[:space:]]*,[[:space:]]*['"][^'"]*['"]"""
    out = subprocess.run(
        ['git', 'grep', '-h', '-o', '-E', pattern, ref, '--', 'backend/*.py', ':!backend/tests'],
        capture_output=True,
        text=True,
    )
    found = [re.split(r"""['"]\s*,\s*['"]""", m, maxsplit=1)[1][:-1] for m in out.stdout.splitlines()]
    if not found or not all(_same(found[0], other) for other in found):
        return None
    return found[0]


def is_switch(name: str, value: str) -> bool:
    """On, or a named mode. Numbers and user lists are tuning."""
    if _USER_LIST.search(name):
        return False
    v = value.strip().lower()
    if v in _TRUE or v == '100':
        return True
    return v not in _FALSE and not re.fullmatch(r'[0-9.]+', v)


def report(old: dict, new: dict, ours: dict, default=lambda name: None) -> list[tuple[str, str, str]]:
    """[(name, upstream value, line)] for settings that need a look."""
    rows = []
    for name in sorted(new):
        value, where = new[name]
        before = old.get(name, (None,))[0]
        if before == value:
            continue
        if name in ours:
            if _same(ours[name], value):
                continue
            fork = f'fork sets {ours[name]!r}'
        else:
            fallback = default(name)
            if fallback is not None and _same(fallback, value):
                continue
            fork = 'not set here, code default ' + (repr(fallback) if fallback is not None else 'not found')
        shown = value if len(value) <= 40 else value[:37] + '...'
        change = f'upstream sets {shown!r}' if before is None else f'upstream {before!r} -> {shown!r}'
        rows.append((name, value, f'  {name}: {change}; {fork} ({", ".join(where)})'))
    return rows


def main(argv: list[str]) -> int:
    show_all = '--all' in argv
    refs = [a for a in argv[1:] if a != '--all']
    if len(refs) != 2:
        print('usage: fork_sync_env_check.py [--all] OLD NEW')
        return 0
    old_ref, new_ref = refs
    new_doc = _show(new_ref, RUNTIME_ENV)
    if new_doc is None:
        print(f'skip upstream switches ({RUNTIME_ENV} is not in {new_ref})')
        return 0
    rows = report(
        switches(_show(old_ref, RUNTIME_ENV)),
        switches(new_doc),
        fork_values(_show('HEAD', FORK_VALUES)),
        default=lambda name: code_default(new_ref, name),
    )
    main_rows = [line for name, value, line in rows if is_switch(name, value)]
    tuning = [line for name, value, line in rows if not is_switch(name, value)]
    if not main_rows and not tuning:
        print('ok   no new upstream server switches')
        return 0
    span = '..'.join(r[:10] if re.fullmatch(r'[0-9a-f]{11,40}', r) else r for r in (old_ref, new_ref))
    print(f'note upstream turned on {len(main_rows)} server switch(es) the fork does not match ({span}).')
    if main_rows:
        print('     For each one, read the code. If the fork needs it, set it:')
        print('\n'.join(main_rows))
    if show_all and tuning:
        print('     tuning:')
        print('\n'.join(tuning))
    elif tuning:
        print(f'     {len(tuning)} tuning setting(s) not shown (numbers, test-user lists). See them: --all')
    print(f'     omi-backend: {FORK_VALUES}')
    print('     pusher, desktop-backend-py: kubectl -n omi set env deploy/<name> NAME=value')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
