"""
Casdoor sign-in router [fork-only]. Mounted in main.py ahead of upstream's
routers/auth.py, so these /v1/auth routes take precedence over the Firebase ones.

Flow:
  1. App calls GET /v1/auth/authorize?redirect_uri=omi://...
  2. Backend stores a session in Redis and redirects user to Casdoor.
  3. Casdoor authenticates the user and redirects back to GET /v1/auth/callback.
  4. Backend exchanges the code for Casdoor tokens, stores them in Redis
     under a short-lived auth code, and redirects to the app's redirect_uri.
  5. App calls POST /v1/auth/token with the auth code to get the id_token.
  6. App uses that id_token as a Bearer token on all subsequent requests.
"""

import base64
import functools
import hashlib
import hmac
import json
import os
import re
import socket
import uuid
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import pathlib

from firebase_admin import _wrapped_token, auth as firebase_auth
from utils.executors import critical_executor, run_blocking
from utils.http_client import get_auth_client
from routers.auth import _build_callback_redirect_url

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


@functools.lru_cache(maxsize=1)
def _casdoor_internal_base() -> str:
    """Internal cluster URL for server-to-server calls. Resolved once per process.

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
    provider: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    """Redirect the user to Casdoor to authenticate.

    ``provider`` (google/apple from upstream's sign-in buttons) is accepted and
    ignored: Casdoor shows its own provider choice. A PKCE ``code_challenge``
    (S256 only) binds the eventual /token call to the app that started the flow.
    """
    del provider
    if not _validate_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    if code_challenge and (code_challenge_method or "").upper() != "S256":
        raise HTTPException(status_code=400, detail="Unsupported code_challenge_method")

    session_id = str(uuid.uuid4())
    set_auth_session(session_id, {"redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge}, 300)

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

    tokens = await _exchange_code_for_tokens(code)

    auth_code = str(uuid.uuid4())
    # Bind the redirect_uri to the code so /token can verify the redeemer
    # presents the same value the flow was started with.
    set_auth_code(
        auth_code,
        json.dumps(
            {"tokens": tokens, "redirect_uri": redirect_uri, "code_challenge": session_data.get("code_challenge")}
        ),
        300,
    )

    return templates.TemplateResponse(
        "auth_callback.html",
        {
            "request": request,
            "code": auth_code,
            "state": session_data.get("state") or "",
            # The page navigates to redirect_url: the app's own scheme (omi://,
            # omi-computer-dev://, ...) with code and state appended. Built by
            # upstream's helper so the page and our router stay in step.
            "redirect_uri": redirect_uri,
            "redirect_url": _build_callback_redirect_url(redirect_uri, auth_code, session_data.get("state")),
        },
    )


# ── 3. Token exchange ────────────────────────────────────────────────────────


@router.post("/token")
async def auth_token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    code_verifier: Optional[str] = Form(None),
    use_custom_token: Optional[str] = Form(None),
):
    """Exchange a short-lived auth code for the Casdoor id_token.

    With ``use_custom_token`` (upstream's desktop app), the response also carries
    a one-time ``custom_token`` that the app redeems at the Firebase REST
    stand-in below (``accounts:signInWithCustomToken``) for the same tokens.
    """
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant type")
    # Direct (non-HTTP) callers leave the optional fields as FastAPI Form markers.
    code_verifier = code_verifier if isinstance(code_verifier, str) else None
    use_custom_token = use_custom_token if isinstance(use_custom_token, str) else None

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
            code_challenge = stored.get("code_challenge")
        else:
            tokens = stored
            bound_redirect_uri = None
            code_challenge = None

        if bound_redirect_uri is not None and not hmac.compare_digest(bound_redirect_uri, redirect_uri):
            raise HTTPException(status_code=400, detail="redirect_uri mismatch")
        if code_challenge and not _pkce_matches(code_verifier, code_challenge):
            raise HTTPException(status_code=400, detail="code_verifier mismatch")

        response = {
            "id_token": tokens["id_token"],
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_type": "Bearer",
            "expires_in": tokens.get("expires_in", 3600),
        }
        if (use_custom_token or "").lower() in ("1", "true", "yes"):
            response["custom_token"] = _mint_custom_token(tokens)
        return response
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error parsing stored tokens: {e}")
        raise HTTPException(status_code=400, detail="Invalid token data")


# ── 4. Token refresh ─────────────────────────────────────────────────────────


@router.post("/refresh")
async def auth_refresh(refresh_token: str = Form(...)):
    """Use a refresh_token to get a new id_token from Casdoor."""
    response = await _post_refresh_token(refresh_token)
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


_CUSTOM_TOKEN_PREFIX = "casdoor-ct."


def _pkce_matches(code_verifier: Optional[str], code_challenge: str) -> bool:
    """RFC 7636 S256: base64url(sha256(verifier)) without padding == challenge."""
    if not code_verifier:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii", errors="ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, code_challenge)


def _mint_custom_token(tokens: dict) -> str:
    """One-time, short-lived handle for the tokens; redeemed by signInWithCustomToken."""
    handle = f"ct_{uuid.uuid4().hex}"
    set_auth_code(handle, json.dumps(tokens), 300)
    return _CUSTOM_TOKEN_PREFIX + handle


def _redeem_custom_token(custom_token: str) -> Optional[dict]:
    if not custom_token or not custom_token.startswith(_CUSTOM_TOKEN_PREFIX):
        return None
    handle = custom_token[len(_CUSTOM_TOKEN_PREFIX) :]
    stored = get_auth_code(handle)
    if not stored:
        return None
    delete_auth_code(handle)
    return json.loads(stored)


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _post_refresh_token(refresh_token: str):
    """Casdoor refresh-token POST on the shared async auth client."""
    return await get_auth_client().post(
        f"{_casdoor_internal_base()}/api/login/oauth/access_token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def _exchange_code_for_tokens(code: str) -> dict:
    """Exchange a Casdoor authorization code for tokens."""
    token_url = f"{_casdoor_internal_base()}/api/login/oauth/access_token"
    response = await get_auth_client().post(
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


# ── Firebase REST stand-in (for upstream's desktop app) ──────────────────────
#
# Upstream's apps finish sign-in against Google's Firebase REST API: they
# redeem the custom_token from /v1/auth/token at identitytoolkit
# (accounts:signInWithCustomToken), read the account (accounts:lookup) and
# refresh at securetoken. The macOS app sends these here when
# OMI_FIREBASE_REST_BASE_URL is set; the phone app does when its Firebase Auth
# SDK is in emulator mode. They are answered from Casdoor, in Firebase's
# response format. ID tokens go out wrapped (firebase_admin/_wrapped_token.py)
# because the phone SDK needs claims that Casdoor tokens lack.

firebase_rest_router = APIRouter(tags=["authentication"])


def _firebase_error(status_code: int, message: str) -> JSONResponse:
    # Firebase REST error envelope; the app keys session death off `message`.
    return JSONResponse(status_code=status_code, content={"error": {"code": status_code, "message": message}})


@firebase_rest_router.post("/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken")
async def firebase_sign_in_with_custom_token(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _firebase_error(400, "INVALID_CUSTOM_TOKEN")
    tokens = _redeem_custom_token(str((body or {}).get("token") or ""))
    if not tokens or not tokens.get("id_token"):
        return _firebase_error(400, "INVALID_CUSTOM_TOKEN")
    try:
        claims = await run_blocking(critical_executor, firebase_auth.verify_id_token, tokens["id_token"])
    except firebase_auth.InvalidIdTokenError:
        return _firebase_error(400, "INVALID_CUSTOM_TOKEN")
    return {
        "kind": "identitytoolkit#VerifyCustomTokenResponse",
        "idToken": _wrapped_token.wrap(tokens["id_token"], claims),
        "refreshToken": tokens.get("refresh_token") or "",
        "expiresIn": str(tokens.get("expires_in", 3600)),
        "localId": claims["uid"],
        "isNewUser": False,
    }


@firebase_rest_router.post("/securetoken.googleapis.com/v1/token")
async def firebase_refresh_token(request: Request):
    # The macOS app posts a form (grant_type); the phone SDK posts JSON (grantType).
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        grant_type, refresh_token = body.get("grantType"), body.get("refreshToken")
    else:
        form = await request.form()
        grant_type, refresh_token = form.get("grant_type"), form.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return _firebase_error(400, "MISSING_REFRESH_TOKEN")
    if grant_type != "refresh_token":
        return _firebase_error(400, "INVALID_GRANT_TYPE")
    try:
        response = await _post_refresh_token(refresh_token)
    except Exception:
        # Casdoor unreachable: not a session death, the app retries later.
        return _firebase_error(503, "UNAVAILABLE")
    tokens = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if response.status_code >= 500:
        return _firebase_error(503, "UNAVAILABLE")
    if response.status_code != 200 or not tokens.get("id_token") or tokens.get("error"):
        return _firebase_error(400, "INVALID_REFRESH_TOKEN")
    try:
        claims = await run_blocking(critical_executor, firebase_auth.verify_id_token, tokens["id_token"])
    except firebase_auth.InvalidIdTokenError:
        return _firebase_error(400, "INVALID_REFRESH_TOKEN")
    id_token = _wrapped_token.wrap(tokens["id_token"], claims)
    return {
        # The phone SDK uses access_token as its ID token.
        "access_token": id_token,
        "expires_in": str(tokens.get("expires_in", 3600)),
        "token_type": "Bearer",
        "refresh_token": tokens.get("refresh_token") or refresh_token,
        "id_token": id_token,
        "user_id": claims["uid"],
        "project_id": "selfhosted",
    }


async def _claims_from_body(request: Request) -> Optional[dict]:
    try:
        body = await request.json()
        return await run_blocking(critical_executor, firebase_auth.verify_id_token, str(body.get("idToken") or ""))
    except Exception:
        return None


def _firebase_user(claims: dict, display_name: Optional[str] = None) -> dict:
    return {
        "localId": claims["uid"],
        "email": claims.get("email") or "",
        "emailVerified": True,
        "displayName": display_name or claims.get("displayName") or claims.get("name") or "",
        "providerUserInfo": [],
    }


@firebase_rest_router.post("/identitytoolkit.googleapis.com/v1/accounts:lookup")
async def firebase_lookup_account(request: Request):
    claims = await _claims_from_body(request)
    if not claims:
        return _firebase_error(400, "INVALID_ID_TOKEN")
    return {"kind": "identitytoolkit#GetAccountInfoResponse", "users": [_firebase_user(claims)]}


@firebase_rest_router.post("/identitytoolkit.googleapis.com/v1/accounts:update")
async def firebase_update_account(request: Request):
    # Profile edits are not stored in Casdoor; echo the name so the SDK call
    # succeeds. The app also saves the name to the backend profile.
    claims = await _claims_from_body(request)
    if not claims:
        return _firebase_error(400, "INVALID_ID_TOKEN")
    body = await request.json()
    display_name = body.get("displayName") if isinstance(body.get("displayName"), str) else None
    return {"kind": "identitytoolkit#SetAccountInfoResponse", **_firebase_user(claims, display_name)}


# The iOS SDK (11.x) uses the older relyingparty paths for the same calls.
_V3 = "/www.googleapis.com/identitytoolkit/v3/relyingparty"
firebase_rest_router.add_api_route(f"{_V3}/verifyCustomToken", firebase_sign_in_with_custom_token, methods=["POST"])
firebase_rest_router.add_api_route(f"{_V3}/getAccountInfo", firebase_lookup_account, methods=["POST"])
firebase_rest_router.add_api_route(f"{_V3}/setAccountInfo", firebase_update_account, methods=["POST"])
