"""
OIDC token verification for Casdoor.

Verifies RS256 JWTs issued by Casdoor using the JWKS endpoint.
Keys are cached by PyJWKClient to avoid fetching on every request.
"""

import os
import socket

import jwt
from jwt import PyJWKClient

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        external_url = os.environ["CASDOOR_ENDPOINT"].rstrip("/")
        internal_url = os.environ.get("CASDOOR_INTERNAL_URL", "").rstrip("/")

        # Prefer internal URL (e.g. K8s cluster DNS) when set — avoids going through
        # a public load balancer for server-to-server calls.  Fall back to the public
        # URL if the internal host is unreachable (e.g. Docker-compose environments).
        if internal_url:
            try:
                host = internal_url.split("//")[-1].split("/")[0].split(":")[0]
                old_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(2)
                try:
                    socket.getaddrinfo(host, None)
                finally:
                    socket.setdefaulttimeout(old_timeout)
                base_url = internal_url
            except (socket.gaierror, OSError, socket.timeout):
                print(f"OIDC: internal URL '{internal_url}' unreachable, falling back to '{external_url}'")
                base_url = external_url
        else:
            base_url = external_url

        jwks_url = f"{base_url}/.well-known/jwks"
        print(f"OIDC: using JWKS URL: {jwks_url}")
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_client


def verify_oidc_token(token: str) -> dict:
    """
    Verify a Casdoor-issued JWT and return its decoded payload.
    Raises jwt.exceptions.* on any verification failure.
    """
    client = _get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(token)
    audience = os.environ["CASDOOR_CLIENT_ID"]
    decoded = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
    )
    return decoded
