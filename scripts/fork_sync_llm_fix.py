#!/usr/bin/env python3
"""[fork-only] Fix merge conflicts with any OpenAI-compatible LLM.

Used by scripts/fork-sync when the `claude` command is not installed.
Standard library only, so it runs before any venv exists.

    scripts/fork_sync_llm_fix.py <file> [<file> ...]

Settings (environment, or .fork-sync.env in the repo root):
    FORK_SYNC_LLM_URL    base URL that ends in /v1   (default: SELF_HOSTED_LLM_URL)
    FORK_SYNC_LLM_KEY    API key                      (default: SELF_HOSTED_LLM_KEY)
    FORK_SYNC_LLM_MODEL  model name                   (default: SELF_HOSTED_LLM_MODEL)
    FORK_SYNC_LLM_MAX_CHARS  skip files larger than this (default 60000)

The script sends each conflicted file to the model and writes the answer back
only when the answer has no conflict markers. It does not stage or commit.
Exit code 1 means at least one file is still unresolved.
"""

import json
import os
import re
import sys
import urllib.request

RULES = """You resolve git merge conflicts in a fork of the Omi project.
The fork runs Omi self-hosted. Its rules:
- Fork code lives in fork-only files, or in a block at the END of an upstream
  file. Such a block replaces an upstream function by name, under an
  environment check such as SELF_HOSTED.
- Upstream code stays byte-identical to upstream outside those blocks.
- When upstream adds code at the end of a file, put upstream's code first and
  the fork block after it.
- When upstream removes or renames a function that a fork block replaces,
  change the fork block to the new name. Do not bring the old upstream code back.
- A fork block must match the upstream function's current signature.
In conflict markers, "ours" (<<<<<<<) is the fork and "theirs" (>>>>>>>) is the
code being merged in (normally upstream).
Answer with the complete resolved file in ONE fenced code block and nothing else.
If you cannot resolve it safely, answer with the single word UNRESOLVED."""

MARKER = re.compile(r'^(<<<<<<<|=======|>>>>>>>)( |$)', re.M)


def setting(name, fallback):
    return os.environ.get(name, '').strip() or os.environ.get(fallback, '').strip()


def ask(url, key, model, path, text):
    body = {
        'model': model,
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': RULES},
            {'role': 'user', 'content': f'File: {path}\n\n{text}'},
        ],
    }
    req = urllib.request.Request(
        url.rstrip('/') + '/chat/completions',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key or "sk-local"}'},
    )
    with urllib.request.urlopen(req, timeout=float(os.environ.get('FORK_SYNC_LLM_TIMEOUT', '600'))) as r:
        return json.load(r)['choices'][0]['message']['content']


def extract(answer):
    blocks = re.findall(r'```[^\n]*\n(.*?)\n```', answer, re.S)
    if not blocks:
        return None
    return max(blocks, key=len) + '\n'


def main(paths):
    url = setting('FORK_SYNC_LLM_URL', 'SELF_HOSTED_LLM_URL')
    key = setting('FORK_SYNC_LLM_KEY', 'SELF_HOSTED_LLM_KEY')
    model = setting('FORK_SYNC_LLM_MODEL', 'SELF_HOSTED_LLM_MODEL')
    if not url or not model:
        print('fork-sync-llm: set FORK_SYNC_LLM_URL and FORK_SYNC_LLM_MODEL (see SELFHOSTED.md)', file=sys.stderr)
        return 2
    limit = int(os.environ.get('FORK_SYNC_LLM_MAX_CHARS', '60000'))
    left = []
    for path in paths:
        with open(path, encoding='utf-8', errors='surrogateescape') as f:
            text = f.read()
        if len(text) > limit:
            print(f'skip {path}: {len(text)} chars is more than FORK_SYNC_LLM_MAX_CHARS={limit}')
            left.append(path)
            continue
        try:
            answer = ask(url, key, model, path, text)
        except Exception as e:
            print(f'fail {path}: {type(e).__name__}: {e}')
            left.append(path)
            continue
        fixed = extract(answer)
        if fixed is None or 'UNRESOLVED' in answer[:40] or MARKER.search(fixed):
            print(f'fail {path}: the model did not give a clean file')
            left.append(path)
            continue
        with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.write(fixed)
        print(f'done {path}')
    if left:
        print('still unresolved: ' + ' '.join(left))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
