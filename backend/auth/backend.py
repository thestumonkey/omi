"""
Casdoor — FastAPI backend auth
================================
JWT validation + user sync for FastAPI services behind a Casdoor-authenticated
frontend.

Flow
----
  Browser → (OAuth code flow) → Casdoor → access_token (JWT)
  Browser → API request with Authorization: Bearer <access_token>
  FastAPI  → validate JWT signature via Casdoor JWKS
           → get-or-create local User from `sub` claim
           → sync roles from `roles` claim

Dependencies
------------
  pip install python-jose[cryptography] httpx fastapi sqlalchemy

Required env vars
-----------------
  CASDOOR_ENDPOINT      Internal URL (server → Casdoor, e.g. http://casdoor:8000)
  CASDOOR_EXTERNAL_URL  Browser-facing URL (must match JWT `iss` claim)
  CASDOOR_CLIENT_ID     Written by casdoor-provision (used as JWT audience)

Role claim
----------
Casdoor can inject a custom `roles` array into the JWT via the token fields
configuration (Admin → Applications → Token fields → add `roles`). Without this
the roles claim is absent and all users get only the base role.

Role names are prefixed with your app name (e.g. "myapp-admin") to avoid
collisions when multiple apps share an org.

Adapt for your project
-----------------------
  1. Replace the User / UserRole / RoleType imports with your own models.
  2. Update _ROLE_MAP with your app-prefixed role names.
  3. Wire get_current_user / require_role into your router dependencies.
  4. Add jwks_url / CASDOOR_CLIENT_ID to your settings object.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# ── Replace these with your own model imports ──────────────────────────────
from ..models.user import RoleType, User, UserRole   # ← adapt
from ..core.config import settings                    # ← adapt
from ..core.database import get_db                    # ← adapt
# ──────────────────────────────────────────────────────────────────────────

# Map Casdoor role names → application RoleType enum values.
# Prefix with your app name so roles don't bleed across apps sharing an org.
_ROLE_MAP: dict[str, RoleType] = {
    "myapp-admin":   RoleType.admin,    # ← adapt
    "myapp-editor":  RoleType.editor,   # ← adapt
}

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------

class JWKSCache:
    """
    In-memory cache for Casdoor's public signing keys.
    Refreshes automatically after TTL or on unknown key ID.
    """

    _TTL = timedelta(hours=1)

    def __init__(self) -> None:
        self._keys: list[dict[str, Any]] = []
        self._fetched_at: datetime | None = None

    def _is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        return datetime.now(timezone.utc) - self._fetched_at > self._TTL

    async def _refresh(self) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.get(settings.jwks_url, timeout=10)
            response.raise_for_status()
        self._keys = response.json().get("keys", [])
        self._fetched_at = datetime.now(timezone.utc)
        logger.info("JWKS refreshed (%d keys)", len(self._keys))

    async def get_key(self, kid: str) -> dict[str, Any]:
        """Return the JWK matching `kid`, refreshing cache if needed."""
        if self._is_stale():
            await self._refresh()

        key = next((k for k in self._keys if k.get("kid") == kid), None)

        # Unknown kid may mean key rotation — try one fresh fetch
        if key is None:
            await self._refresh()
            key = next((k for k in self._keys if k.get("kid") == kid), None)

        if key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown token signing key",
            )
        return key


_jwks_cache = JWKSCache()


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

async def _validate_token(token: str) -> dict[str, Any]:
    """Validate a raw JWT string. Returns decoded claims on success."""
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format",
        ) from exc

    kid = header.get("kid", "")
    raw_key = await _jwks_cache.get_key(kid)

    issuer = (settings.CASDOOR_EXTERNAL_URL or settings.CASDOOR_ENDPOINT).rstrip("/")

    try:
        claims = jwt.decode(
            token,
            raw_key,
            algorithms=["RS256"],
            audience=settings.CASDOOR_CLIENT_ID,
            issuer=issuer,
        )
    except JWTError as exc:
        unverified = jwt.get_unverified_claims(token)
        logger.warning(
            "Token validation failed: %s | iss=%s aud=%s",
            exc, unverified.get("iss"), unverified.get("aud"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token validation failed",
        ) from exc

    return claims


# ---------------------------------------------------------------------------
# User sync
# ---------------------------------------------------------------------------

async def _get_or_create_user(claims: dict[str, Any], db: AsyncSession) -> User:
    """
    Look up the User by their Casdoor `sub` claim.
    Creates a new User row on first login; syncs profile and roles on every request.

    Roles are derived from the `roles` claim injected by Casdoor's token
    configuration. The database is the runtime authority for permission checks;
    Casdoor role membership is the source of truth that keeps it up to date.
    """
    sub = claims["sub"]

    # Casdoor can return roles as plain strings or objects with a "name" key
    roles_claim = claims.get("roles")
    roles_configured = roles_claim is not None
    role_names: list[str] = [
        r["name"] if isinstance(r, dict) else r
        for r in (roles_claim or [])
    ]
    desired_roles: set[RoleType] = {
        _ROLE_MAP[r] for r in role_names if r in _ROLE_MAP
    }
    desired_roles.add(RoleType.user)  # everyone always gets the base role

    result = await db.execute(
        select(User)
        .where(User.oidc_sub == sub)
        .options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()

    # Migration path: user existed under a previous IdP (different sub, same email)
    if user is None:
        email = claims.get("email", "")
        username = claims.get("name", "")
        for field, value in [("email", email), ("username", username)]:
            if not value:
                continue
            col = User.email if field == "email" else User.username
            result = await db.execute(
                select(User).where(col == value).options(selectinload(User.roles))
            )
            user = result.scalar_one_or_none()
            if user is not None:
                user.oidc_sub = sub
                await db.commit()
                logger.info("Migrated existing user %s to new oidc_sub", user.username)
                break

    if user is None:
        user = User(
            oidc_sub=sub,
            email=claims.get("email", ""),
            username=claims.get("name", sub),
            display_name=claims.get("displayName") or claims.get("name"),
        )
        db.add(user)
        await db.flush()
        for role in desired_roles:
            db.add(UserRole(user_id=user.id, role=role))
        await db.commit()
        logger.info("Created new user: %s", user.username)
    else:
        user.email = claims.get("email", user.email)
        user.display_name = (
            claims.get("displayName") or claims.get("name") or user.display_name
        )

        current_roles = {r.role for r in user.roles}
        for role in desired_roles - current_roles:
            db.add(UserRole(user_id=user.id, role=role))
        if roles_configured:
            for user_role in user.roles:
                if user_role.role not in desired_roles:
                    await db.delete(user_role)

        await db.commit()

    result = await db.execute(
        select(User)
        .where(User.oidc_sub == sub)
        .options(selectinload(User.roles))
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Dependency: validates the Bearer token and returns the current User.
    Raises 401 if the token is missing or invalid.

    Usage:
        @router.get("/me")
        async def me(user: User = Depends(get_current_user)):
            ...
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = await _validate_token(credentials.credentials)
    return await _get_or_create_user(claims, db)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    Dependency: like get_current_user but returns None for unauthenticated
    requests. Use for endpoints that show different content to logged-in vs
    anonymous users.
    """
    if credentials is None:
        return None
    try:
        claims = await _validate_token(credentials.credentials)
        return await _get_or_create_user(claims, db)
    except HTTPException:
        return None


def require_role(*roles: RoleType):
    """
    Dependency factory: ensures the current user has at least one of the
    specified roles. Raises 403 otherwise.

    Usage:
        @router.post("/events")
        async def create_event(
            user: User = Depends(require_role(RoleType.editor, RoleType.admin))
        ):
            ...
    """
    async def _check(user: User = Depends(get_current_user)) -> User:
        await user.awaitable_attrs.roles
        if not any(user.has_role(r) for r in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return _check
