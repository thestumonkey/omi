"""``firebase_admin.auth`` stand-in backed by Casdoor. [fork-only]

ID tokens are Casdoor OIDC JWTs (RS256, checked against Casdoor's JWKS). The
decoded claims gain a ``uid`` key equal to ``sub``, which is what every upstream
caller reads. User records come from Casdoor's management API.

Firebase-only operations (custom tokens, creating or editing users) raise
UnsupportedOperationError: in self-hosted, users sign up in Casdoor and the
upstream flows that mint Firebase custom tokens are replaced by our Casdoor
router (routers/casdoor_auth.py).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import jwt
import requests

from firebase_admin import _casdoor
from firebase_admin.exceptions import InvalidArgumentError, NotFoundError, UnavailableError, UnknownError

# ── Errors (same names and hierarchy as the real SDK) ────────────────────────


class InvalidIdTokenError(InvalidArgumentError):
    pass


class ExpiredIdTokenError(InvalidIdTokenError):
    pass


class RevokedIdTokenError(InvalidIdTokenError):
    pass


class UserDisabledError(InvalidArgumentError):
    pass


class CertificateFetchError(UnknownError):
    pass


class UserNotFoundError(NotFoundError):
    pass


class UnsupportedOperationError(RuntimeError):
    """A Firebase Auth operation that has no Casdoor equivalent here."""


# ── User records ─────────────────────────────────────────────────────────────


class UserMetadata:
    def __init__(self, creation_timestamp: Optional[int] = None, last_sign_in_timestamp: Optional[int] = None):
        # Epoch milliseconds, like the real SDK.
        self.creation_timestamp = creation_timestamp
        self.last_sign_in_timestamp = last_sign_in_timestamp
        self.last_refresh_timestamp = None


class UserRecord:
    def __init__(self, uid: str, casdoor_user: Dict[str, Any]):
        self.uid = uid
        self.email = casdoor_user.get("email") or None
        self.email_verified = bool(casdoor_user.get("emailVerified", True))
        self.phone_number = casdoor_user.get("phone") or None
        self.display_name = casdoor_user.get("displayName") or casdoor_user.get("name") or None
        self.photo_url = casdoor_user.get("avatar") or None
        self.disabled = bool(casdoor_user.get("isForbidden")) or not casdoor_user.get("isEnabled", True)
        self.user_metadata = UserMetadata(creation_timestamp=_epoch_ms(casdoor_user.get("createdTime")))
        self.custom_claims = None
        self.provider_id = "casdoor"
        self.provider_data: List[Any] = []
        self.tenant_id = None
        self._casdoor_user = casdoor_user


def _epoch_ms(iso_time: Optional[str]) -> Optional[int]:
    if not iso_time:
        return None
    try:
        return int(datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# ── Token verification ───────────────────────────────────────────────────────


def verify_id_token(
    id_token: str, app: Any = None, check_revoked: bool = False, clock_skew_seconds: int = 0
) -> Dict[str, Any]:
    # Imported here so this module loads without CASDOOR_* set (tests, tools).
    from utils.oidc import verify_oidc_token

    if not id_token or not isinstance(id_token, str):
        raise InvalidIdTokenError("ID token must be a non-empty string")
    if not _casdoor.configured():
        # An InvalidIdTokenError (not a crash) keeps upstream's LOCAL_DEVELOPMENT
        # fallback working on a machine with no Casdoor.
        raise InvalidIdTokenError("Casdoor is not configured (CASDOOR_ENDPOINT unset)")
    try:
        claims = verify_oidc_token(id_token)
    except jwt.ExpiredSignatureError as e:
        raise ExpiredIdTokenError("Token expired", cause=e) from e
    except jwt.PyJWKClientConnectionError as e:
        raise CertificateFetchError(f"Could not fetch Casdoor signing keys: {e}", cause=e) from e
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as e:
        raise InvalidIdTokenError(f"Invalid token: {e}", cause=e) from e
    # check_revoked is accepted but not enforced: Casdoor tokens are
    # short-lived and there is no revocation list to consult.
    claims.setdefault("uid", claims.get("sub"))
    return claims


# ── User management ──────────────────────────────────────────────────────────


def _fetch(uid: str) -> Dict[str, Any]:
    if not uid:
        raise ValueError("uid must be a non-empty string")
    try:
        user = _casdoor.get_user(uid)
    except requests.RequestException as e:
        raise UnavailableError(f"Casdoor lookup failed for {uid}: {e}", cause=e) from e
    if not user:
        raise UserNotFoundError(f"No user record found for the provided user ID: {uid}")
    return user


def get_user(uid: str, app: Any = None) -> UserRecord:
    return UserRecord(uid, _fetch(uid))


def get_user_by_email(email: str, app: Any = None) -> UserRecord:
    try:
        user = _casdoor.get_user_by_email(email)
    except requests.RequestException as e:
        raise UnavailableError(f"Casdoor lookup failed for {email}: {e}", cause=e) from e
    if not user:
        raise UserNotFoundError(f"No user record found for the provided email: {email}")
    uid = f"{user.get('owner')}/{user.get('name')}"
    return UserRecord(uid, user)


def delete_user(uid: str, app: Any = None) -> None:
    user = _fetch(uid)
    try:
        _casdoor.delete_user(user)
    except requests.RequestException as e:
        raise UnavailableError(f"Casdoor delete failed for {uid}: {e}", cause=e) from e


def _unsupported(name: str):
    def call(*_args: Any, **_kwargs: Any) -> Any:
        raise UnsupportedOperationError(f"firebase_admin.auth.{name} is not available with Casdoor auth")

    call.__name__ = name
    return call


# Upstream's local-QA mutation guard (utils/firebase_admin_runtime.py) checks
# that every one of these names exists before it swaps them out, so all must be
# defined even though none has a Casdoor equivalent here.
create_custom_token = _unsupported("create_custom_token")
create_oidc_provider_config = _unsupported("create_oidc_provider_config")
create_saml_provider_config = _unsupported("create_saml_provider_config")
create_session_cookie = _unsupported("create_session_cookie")
create_user = _unsupported("create_user")
delete_oidc_provider_config = _unsupported("delete_oidc_provider_config")
delete_saml_provider_config = _unsupported("delete_saml_provider_config")
delete_users = _unsupported("delete_users")
generate_email_verification_link = _unsupported("generate_email_verification_link")
generate_password_reset_link = _unsupported("generate_password_reset_link")
generate_sign_in_with_email_link = _unsupported("generate_sign_in_with_email_link")
import_users = _unsupported("import_users")
revoke_refresh_tokens = _unsupported("revoke_refresh_tokens")
set_custom_user_claims = _unsupported("set_custom_user_claims")
update_oidc_provider_config = _unsupported("update_oidc_provider_config")
update_saml_provider_config = _unsupported("update_saml_provider_config")
update_user = _unsupported("update_user")
