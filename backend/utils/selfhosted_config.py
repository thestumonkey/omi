"""Server-wide settings for the self-hosted fork, changed from the apps. [fork-only]

Each setting group is one Mongo document, selfhosted_settings/{name}, so every
process (omi-backend, pusher, desktop backend) sees the same choice. Reads are
cached per process for CACHE_SECONDS; a write updates this process at once.

Groups:
  llm    {"model": str | None}           chat model (utils/llm/selfhosted.py)
  voice  {"mode": "off" | "cloud"}       live voice + push-to-talk relay
                                         (tail blocks of routers/desktop_realtime.py
                                         and routers/omni_relay.py)
"""

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0
_COLLECTION = 'selfhosted_settings'
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

VOICE_OFF, VOICE_CLOUD = 'off', 'cloud'
VOICE_MODES = (VOICE_OFF, VOICE_CLOUD)


def is_self_hosted() -> bool:
    return os.environ.get('SELF_HOSTED', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _ref(name: str):
    from database._client import db

    return db.collection(_COLLECTION).document(name)


def _load(name: str) -> Optional[Dict[str, Any]]:
    """The stored document, {} when absent, None when it cannot be read."""
    if not os.environ.get('MONGODB_URL', '').strip():
        return {}  # no shim database (unit tests, cloud deploys)
    try:
        snapshot = _ref(name).get()
    except Exception as e:  # a settings read must never break the caller
        logger.warning('self-hosted settings: cannot read %s: %s', name, e)
        return None
    return (snapshot.to_dict() or {}) if snapshot.exists else {}


def read(name: str) -> Dict[str, Any]:
    now = time.monotonic()
    cached = _cache.get(name)
    if cached is not None and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    data = _load(name)
    if data is None:  # keep the last good value through a database blip
        data = cached[1] if cached is not None else {}
    _cache[name] = (now, data)
    return data


def write(name: str, data: Dict[str, Any]) -> None:
    stored = {**data, 'updated_at': time.time()}
    _ref(name).set(stored)
    _cache[name] = (time.monotonic(), stored)


def voice_mode() -> str:
    """Live voice mode. Self-hosted servers default to off: the providers are cloud-only."""
    mode = read('voice').get('mode')
    if mode in VOICE_MODES:
        return mode
    return VOICE_OFF if is_self_hosted() else VOICE_CLOUD
