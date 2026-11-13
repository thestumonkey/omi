"""[fork-only] Check that the self-hosted fork still boots after a sync.

Run from backend/ with the backend venv:

    .venv/bin/python scripts/selfhosted_boot_check.py

It needs no real services. It sets fake addresses for MongoDB, MinIO, Casdoor,
Typesense and the LLM, imports the three servers, and checks that each fork
seam is still in place. It exits 1 and prints FAIL lines when a check fails.

Upstream changes code paths often without a merge conflict (for example the
2026-11 change that sent every LLM call to upstream's gateway). These checks
catch that kind of break, which the merge itself does not show.
"""

import os
import subprocess
import sys

FAKE_ENV = {
    'SELF_HOSTED': 'true',
    'OMI_ENV_STAGE': 'local',
    'MONGODB_URL': 'mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=500',
    'MONGODB_DB': 'omi',
    'S3_ENDPOINT_URL': 'http://127.0.0.1:1',
    'S3_ACCESS_KEY_ID': 'x',
    'S3_SECRET_ACCESS_KEY': 'y',
    'CASDOOR_ENDPOINT': 'http://127.0.0.1:1',
    'CASDOOR_CLIENT_ID': 'x',
    'CASDOOR_CLIENT_SECRET': 'y',
    'SELF_HOSTED_VECTOR_STORE': 'typesense',
    'TYPESENSE_HOST': '127.0.0.1',
    'TYPESENSE_HOST_PORT': '1',
    'TYPESENSE_PROTOCOL': 'http',
    'TYPESENSE_API_KEY': 'x',
    'SELF_HOSTED_LLM_URL': 'http://127.0.0.1:1/v1',
    'SELF_HOSTED_LLM_MODEL': 'local-model',
    'SELF_HOSTED_EMBED_URL': 'http://127.0.0.1:1/v1',
    'SELF_HOSTED_EMBED_MODEL': 'nomic-embed-text',
    'ENCRYPTION_SECRET': 'omi_boot_check_secret_value_1234567890abcdef',
    'STT_SERVICE_MODELS': 'dg-nova-3',
    'DEEPGRAM_API_KEY': 'x',
    'REDIS_DB_HOST': '127.0.0.1',
    'REDIS_DB_PORT': '1',
}

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=''):
    print(('ok   ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))
    if not ok:
        failures.append(name)


def run_check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # report every check, do not stop at the first error
        ok, detail = False, f'{type(e).__name__}: {str(e)[:200]}'
    check(name, ok, detail)


def import_only(module):
    """Import a second server in its own process, so its modules do not mix with main."""
    r = subprocess.run(
        [sys.executable, '-c', f'import {module}; print(len({module}.app.routes))'],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        return False, (r.stderr.strip().splitlines() or ['no output'])[-1][:200]
    return True, f'{r.stdout.strip().splitlines()[-1]} routes'


def main():
    for k, v in FAKE_ENV.items():
        os.environ.setdefault(k, v)
    os.environ['PYTHONPATH'] = BACKEND_DIR
    sys.path.insert(0, BACKEND_DIR)

    import main as backend_main  # noqa: E402  (needs the env above)

    app = backend_main.app
    check('backend imports', True, f'{len(app.routes)} routes')

    def health():
        from fastapi.testclient import TestClient

        code = TestClient(app).get('/v1/health').status_code
        return code == 200, f'status {code}'

    def casdoor_routes():
        # Upstream's auth router also serves these paths. The app uses the
        # first route that matches, so the Casdoor route must come first.
        found = {}
        for r in app.routes:
            if getattr(r, 'path', None) in ('/v1/auth/token', '/v1/auth/authorize'):
                found.setdefault(r.path, r.endpoint.__module__)
        ok = len(found) == 2 and all(m == 'routers.casdoor_auth' for m in found.values())
        return ok, str(found)

    def firebase_stand_in():
        import firebase_admin

        path = os.path.abspath(firebase_admin.__file__)
        return path.startswith(os.path.join(BACKEND_DIR, 'firebase_admin')), path

    def mongo_client():
        # `_client.db` is a lazy proxy, so check the client that it makes.
        from database import _client

        names = {
            'db': type(_client.get_firestore_client()).__name__,
            'data_plane_db': type(_client.get_data_plane_firestore_client()).__name__,
        }
        return all(n == 'MongoFirestore' for n in names.values()), str(names)

    def local_llm():
        from utils.llm.clients import get_llm, get_llm_gateway_chat_structured

        names = {f: type(get_llm(f)).__name__ for f in ('memory_l2', 'conv_structure', 'chat_responses')}
        names['structured'] = type(get_llm_gateway_chat_structured()).__name__
        return all(n == 'SelfHostedChatOpenAI' for n in names.values()), str(names)

    def speech_to_text():
        # Upstream retired Deepgram in 2026-10 with no merge conflict for the fork.
        from utils.stt import pre_recorded
        from utils.stt.streaming import get_stt_service_for_language

        service, _, model = get_stt_service_for_language('en')
        live = f'{getattr(service, "value", service)} {model}'
        upload = type(pre_recorded._deepgram_client_for_request()).__name__
        return live.startswith('deepgram') and upload == 'DeepgramClient', f'live={live} upload={upload}'

    run_check('GET /v1/health gives 200', health)
    run_check('speech-to-text uses Deepgram (live and uploads)', speech_to_text)
    run_check('Casdoor sign-in routes', casdoor_routes)
    run_check('firebase_admin is the fork stand-in', firebase_stand_in)
    run_check('database client is MongoFirestore', mongo_client)
    run_check('LLM calls go to SELF_HOSTED_LLM_URL', local_llm)
    run_check('pusher imports', lambda: import_only('pusher.main'))
    run_check('desktop backend imports', lambda: import_only('desktop_backend'))

    if failures:
        print(f'\n{len(failures)} check(s) failed: ' + ', '.join(failures))
        return 1
    print('\nall boot checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
