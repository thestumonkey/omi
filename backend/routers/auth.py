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

import json
import os
import socket
import uuid
from typing import Optional
from urllib.parse import quote

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


# ── 1. Start sign-in ─────────────────────────────────────────────────────────


@router.get("/authorize")
async def auth_authorize(
    request: Request,
    redirect_uri: str,
    state: Optional[str] = None,
):
    """Redirect the user to Casdoor to authenticate."""
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

    tokens = _exchange_code_for_tokens(code)

    auth_code = str(uuid.uuid4())
    set_auth_code(auth_code, json.dumps(tokens), 300)

    return templates.TemplateResponse(
        "auth_callback.html",
        {
            "request": request,
            "code": auth_code,
            "state": session_data.get("state") or "",
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

    tokens_json = get_auth_code(code)
    if not tokens_json:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    delete_auth_code(code)

    try:
        tokens = json.loads(tokens_json)
        return {
            "id_token": tokens["id_token"],
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_type": "Bearer",
            "expires_in": tokens.get("expires_in", 3600),
        }
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
