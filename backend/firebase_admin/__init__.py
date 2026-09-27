"""Self-hosted stand-in for the ``firebase_admin`` SDK. [fork-only]

Upstream omi imports ``firebase_admin`` in ~30 live modules. This package sits
in ``backend/`` so it shadows the real SDK on ``sys.path`` and lets those
modules run byte-identical to upstream while talking to our own stack:

    firebase_admin.auth       -> Casdoor (OIDC token checks + management API)
    firebase_admin.firestore  -> MongoDB, through database/mongo_firestore.py
    firebase_admin.messaging  -> no-op push (no FCM project in self-hosted)
    initialize_app/credentials -> accepted and ignored; nothing to connect to

Only the surface upstream actually calls is implemented. When upstream starts
calling something new, add it here rather than editing the upstream caller.
"""

from typing import Any, Dict, Optional

from firebase_admin import credentials  # noqa: F401  (re-exported like the real SDK)

__version__ = "0.0.0-selfhosted"

_DEFAULT_APP_NAME = "[DEFAULT]"


class App:
    def __init__(self, name: str, credential: Any, options: Optional[Dict[str, Any]]):
        self.name = name
        self.credential = credential
        self.options = dict(options or {})
        self.project_id = self.options.get("projectId")


_apps: Dict[str, App] = {}


def initialize_app(
    credential: Any = None, options: Optional[Dict[str, Any]] = None, name: str = _DEFAULT_APP_NAME
) -> App:
    # Upstream calls this once per process at boot, sometimes from several
    # entry points; the real SDK raises on a duplicate name, which only matters
    # when it would open a second Google connection. Here it is idempotent.
    app = _apps.get(name)
    if app is None:
        app = App(name, credential, options)
        _apps[name] = app
    return app


def get_app(name: str = _DEFAULT_APP_NAME) -> App:
    if name not in _apps:
        raise ValueError(f'The app "{name}" does not exist. Call initialize_app() first.')
    return _apps[name]


def delete_app(app: App) -> None:
    _apps.pop(app.name, None)
