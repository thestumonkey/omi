"""
Auth router — OIDC sign-in flow via Casdoor.

Flow:
  1. App calls GET /v1/auth/authorize?redirect_uri=omi://...
  2. Backend stores a session in Redis and redirects user to Casdoor.
  3. Casdoor authenticates the user and redirects back to GET /v1/auth/callback.
  4. Backend exchanges the code for Casdoor tokens, stores them in Redis
     under a short-lived auth code, and redirects to the app's redirect_uri.
  5. App calls POST /v1/auth/token with the auth code to get the id_token.
  6. App uses that id_token as a Bearer token on all subsequent requests.
"""

import hmac
import json
import os
import re
import socket
import uuid
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import pathlib

from database.redis_db import (
    delete_auth_code,
    get_auth_code,
    get_auth_session,
    set_auth_code,
    set_auth_session,
)

router = APIRouter(
    prefix="/v1/auth",
    tags=["authentication"],
)

templates_path = pathlib.Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))


def _casdoor_base() -> str:
    return os.environ["CASDOOR_ENDPOINT"].rstrip("/")


def _casdoor_internal_base() -> str:
    """Internal cluster URL for server-to-server calls.

    Prefers CASDOOR_INTERNAL_URL (e.g. K8s cluster DNS) when the host is
    resolvable; falls back to the public CASDOOR_ENDPOINT for Docker/Cloud Run
    environments where the internal hostname does not exist.
    """
    external = _casdoor_base()
    internal = os.environ.get("CASDOOR_INTERNAL_URL", "").rstrip("/")
    if not internal:
        return external
    try:
        host = internal.split("//")[-1].split("/")[0].split(":")[0]
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(2)
        try:
            socket.getaddrinfo(host, None)
        finally:
            socket.setdefaulttimeout(old_timeout)
        return internal
    except (socket.gaierror, OSError, socket.timeout):
        print(f"auth: internal Casdoor URL '{internal}' unreachable, using '{external}'")
        return external


def _client_id() -> str:
    return os.environ["CASDOOR_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["CASDOOR_CLIENT_SECRET"]


def _callback_url() -> str:
    base = os.environ["BASE_API_URL"].rstrip("/")
    return f"{base}/v1/auth/callback"


# Native-app custom schemes the omi clients register, mirroring the allowlist
# documented in templates/auth_callback.html:
#   omi://            mobile (Flutter)
#   omi-computer://   desktop prod ; omi-computer-dev:// desktop dev
#   omi-<bundle>://   named desktop builds (the "omi-{anything}" convention)
#   com.omi.app://    reverse-DNS form (RFC 8252-recommended)
# Matched ASCII-only (RFC 3986 scheme grammar) so look-alike unicode schemes
# (e.g. cyrillic "оmi") are rejected. http loopback is permitted for the
# desktop/CLI flow (a localhost server receives the code); https never is
# (RFC 8252 §7.3).
_OMI_SCHEME_RE = re.compile(r"^omi(-[a-z0-9]+)*$")
_ALLOWED_EXACT_SCHEMES = {"com.omi.app"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Allowlist redirect targets so an auth code can never be delivered to an
    attacker-controlled URL. Enforced server-side (the template's client-side
    check is not a security boundary)."""
    if not redirect_uri or not redirect_uri.strip():
        return False
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return False
    if _OMI_SCHEME_RE.match(scheme) or scheme in _ALLOWED_EXACT_SCHEMES:
        return True
    if scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS:
        return True
    return False


# ── 1. Start sign-in ─────────────────────────────────────────────────────────


@router.get("/authorize")
async def auth_authorize(
    request: Request,
    redirect_uri: str,
    state: Optional[str] = None,
):
    """Redirect the user to Casdoor to authenticate."""
    if not _validate_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    session_id = str(uuid.uuid4())
    set_auth_session(session_id, {"redirect_uri": redirect_uri, "state": state}, 300)

    auth_url = (
        f"{_casdoor_base()}/login/oauth/authorize?"
        f"client_id={quote(_client_id())}&"
        f"redirect_uri={quote(_callback_url())}&"
        f"response_type=code&"
        f"scope={quote('openid email profile offline_access')}&"
        f"state={quote(session_id)}"
    )
    return RedirectResponse(url=auth_url)


# ── 2. Casdoor callback ──────────────────────────────────────────────────────


@router.get("/callback")
async def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Receive code from Casdoor, exchange for tokens, redirect to app."""
    if error:
        raise HTTPException(status_code=400, detail=f"Auth error: {error}")

    session_data = get_auth_session(state)
    if not session_data:
        raise HTTPException(status_code=400, detail="Invalid or expired auth session")

    redirect_uri = session_data.get("redirect_uri") or ""

    tokens = _exchange_code_for_tokens(code)

    auth_code = str(uuid.uuid4())
    # Bind the redirect_uri to the code so /token can verify the redeemer
    # presents the same value the flow was started with.
    set_auth_code(auth_code, json.dumps({"tokens": tokens, "redirect_uri": redirect_uri}), 300)

    return templates.TemplateResponse(
        "auth_callback.html",
        {
            "request": request,
            "code": auth_code,
            "state": session_data.get("state") or "",
            "redirect_uri": redirect_uri,
        },
    )


# ── 3. Token exchange ────────────────────────────────────────────────────────


@router.post("/token")
async def auth_token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
):
    """Exchange a short-lived auth code for the Casdoor id_token."""
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant type")

    stored_json = get_auth_code(code)
    if not stored_json:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    # Single-use: consume the code before any further check so a mismatched or
    # malformed attempt cannot be retried.
    delete_auth_code(code)

    try:
        stored = json.loads(stored_json)
        # New format binds redirect_uri; tolerate legacy codes (tokens stored
        # directly) still in Redis from before this change rolled out.
        if isinstance(stored, dict) and "tokens" in stored:
            tokens = stored["tokens"]
            bound_redirect_uri = stored.get("redirect_uri") or ""
        else:
            tokens = stored
            bound_redirect_uri = None

        if bound_redirect_uri is not None and not hmac.compare_digest(bound_redirect_uri, redirect_uri):
            raise HTTPException(status_code=400, detail="redirect_uri mismatch")

        return {
            "id_token": tokens["id_token"],
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_type": "Bearer",
            "expires_in": tokens.get("expires_in", 3600),
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error parsing stored tokens: {e}")
        raise HTTPException(status_code=400, detail="Invalid token data")


# ── 4. Token refresh ─────────────────────────────────────────────────────────


@router.post("/refresh")
async def auth_refresh(refresh_token: str = Form(...)):
    """Use a refresh_token to get a new id_token from Casdoor."""
    token_url = f"{_casdoor_internal_base()}/api/login/oauth/access_token"
    response = requests.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Failed to refresh token")

    tokens = response.json()
    return {
        "id_token": tokens["id_token"],
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_type": "Bearer",
        "expires_in": tokens.get("expires_in", 3600),
    }


# ── Internal helpers ─────────────────────────────────────────────────────────


def _exchange_code_for_tokens(code: str) -> dict:
    """Exchange a Casdoor authorization code for tokens."""
    token_url = f"{_casdoor_internal_base()}/api/login/oauth/access_token"
    response = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _callback_url(),
            "client_id": _client_id(),
            "client_secret": _client_secret(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        print(f"Casdoor token exchange failed: {response.text}")
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code")

    return response.json()
