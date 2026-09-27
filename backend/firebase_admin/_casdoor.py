"""Casdoor management API client behind the ``firebase_admin.auth`` shim. [fork-only]

Needs CASDOOR_ENDPOINT, CASDOOR_CLIENT_ID and CASDOOR_CLIENT_SECRET. Optional:
CASDOOR_INTERNAL_URL (in-cluster address for server-to-server calls) and
CASDOOR_ORG_NAME (owner used when a uid is a bare Casdoor user id).
"""

import os
from typing import Any, Dict, Optional

import requests

_TIMEOUT_SECONDS = 5


def configured() -> bool:
    return bool(os.environ.get("CASDOOR_ENDPOINT", "").strip())


def _base_url() -> str:
    internal = os.environ.get("CASDOOR_INTERNAL_URL", "").strip()
    return (internal or os.environ.get("CASDOOR_ENDPOINT", "")).rstrip("/")


def _auth_params() -> Dict[str, str]:
    return {
        "clientId": os.environ.get("CASDOOR_CLIENT_ID", ""),
        "clientSecret": os.environ.get("CASDOOR_CLIENT_SECRET", ""),
    }


def _lookup_params(uid: str) -> Dict[str, str]:
    # A token's `sub` is either "owner/name" or Casdoor's bare user id,
    # depending on how the application is set up. get-user takes each form
    # under a different parameter.
    if "/" in uid:
        return {"id": uid}
    params = {"userId": uid}
    owner = os.environ.get("CASDOOR_ORG_NAME", "").strip()
    if owner:
        params["owner"] = owner
    return params


def get_user(uid: str) -> Optional[Dict[str, Any]]:
    """The raw Casdoor user object, or None when no such user exists.

    Raises requests.RequestException when Casdoor cannot be reached.
    """
    resp = requests.get(
        f"{_base_url()}/api/get-user",
        params={**_auth_params(), **_lookup_params(uid)},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    user = body.get("data") if isinstance(body, dict) else None
    return user or None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    params = {**_auth_params(), "email": email}
    owner = os.environ.get("CASDOOR_ORG_NAME", "").strip()
    if owner:
        params["owner"] = owner
    resp = requests.get(f"{_base_url()}/api/get-user", params=params, timeout=_TIMEOUT_SECONDS)
    resp.raise_for_status()
    body = resp.json()
    user = body.get("data") if isinstance(body, dict) else None
    return user or None


def delete_user(user: Dict[str, Any]) -> None:
    """Delete a user given its Casdoor object (which carries owner and name)."""
    resp = requests.post(
        f"{_base_url()}/api/delete-user",
        params=_auth_params(),
        json={"owner": user.get("owner"), "name": user.get("name")},
        timeout=_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("status") == "error":
        raise RuntimeError(f"Casdoor delete-user failed: {body.get('msg')}")
