"""Firebase-shaped outer token around a Casdoor ID token. [fork-only]

Firebase's mobile SDK reads the ID token's claims on the device, and it rejects
a token without ``auth_time`` and ``firebase.sign_in_provider``. Casdoor's
tokens have neither. So the sign-in stand-ins (routers/casdoor_auth.py) give
the apps an outer JWT that has these claims and carries the Casdoor token
inside it.

The outer token is unsigned and is never trusted. ``verify_id_token`` unwraps
it and checks the inner Casdoor token's signature as before; the claims it
returns come only from that inner token.
"""

import base64
import json
from typing import Any, Dict

_INNER_CLAIM = "casdoor_id_token"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _encode(value: Dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def wrap(casdoor_id_token: str, claims: Dict[str, Any]) -> str:
    """Wrap a verified Casdoor token. ``claims`` are its verified claims."""
    issued_at = int(claims.get("iat") or 0)
    payload = {
        "iss": claims.get("iss"),
        "aud": claims.get("aud"),
        "sub": claims["uid"],
        "user_id": claims["uid"],
        "email": claims.get("email") or None,
        "name": claims.get("displayName") or claims.get("name") or None,
        "iat": issued_at,
        "auth_time": issued_at,
        "exp": int(claims["exp"]),
        "firebase": {"sign_in_provider": "custom", "identities": {}},
        _INNER_CLAIM: casdoor_id_token,
    }
    return f"{_encode({'alg': 'none', 'typ': 'JWT'})}.{_encode(payload)}."


def unwrap(token: str) -> str:
    """The Casdoor token inside a wrapped token; any other token unchanged."""
    parts = token.split(".")
    if len(parts) != 3 or parts[2]:
        return token
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return token
    inner = payload.get(_INNER_CLAIM) if isinstance(payload, dict) else None
    return inner if isinstance(inner, str) and inner else token
