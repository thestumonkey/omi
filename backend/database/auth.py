"""
User profile lookup via Casdoor.

Uses Casdoor's management API for user profile lookups.
Requires CASDOOR_ENDPOINT, CASDOOR_CLIENT_ID, and CASDOOR_CLIENT_SECRET env vars.

Casdoor's sub claim format is "{org_name}/{username}", so we can parse it
directly to build the user lookup id.
"""

import os
from datetime import datetime

import requests

from database.redis_db import cache_user_name, get_cached_user_name


def _casdoor_params() -> dict:
    """Query params for Casdoor admin API authentication."""
    return {
        "clientId": os.environ.get("CASDOOR_CLIENT_ID", ""),
        "clientSecret": os.environ.get("CASDOOR_CLIENT_SECRET", ""),
    }


def get_user_from_uid(uid: str) -> dict | None:
    """Fetch user profile from Casdoor by their sub (uid).

    The Casdoor sub is "{org_name}/{username}", used directly as the user id.
    """
    if not uid:
        return None

    base = os.environ.get("CASDOOR_ENDPOINT", "").rstrip("/")
    if not base:
        return None

    try:
        params = _casdoor_params()
        params["id"] = uid  # Casdoor id format: "org/username"
        resp = requests.get(
            f"{base}/api/get-user",
            params=params,
            timeout=5,
        )
        resp.raise_for_status()
        body = resp.json()
        user = body.get("data") if isinstance(body, dict) else None
        if not user:
            return None

        return {
            "uid": uid,
            "email": user.get("email"),
            "email_verified": True,
            "phone_number": user.get("phone"),
            "display_name": user.get("displayName") or user.get("name"),
            "photo_url": user.get("avatar"),
            "disabled": not user.get("isEnabled", True),
        }
    except Exception as e:
        print(f"Error fetching user {uid} from Casdoor: {e}")
        return None


def get_user_name(uid: str, use_default: bool = True) -> str | None:
    if cached_name := get_cached_user_name(uid):
        return cached_name

    default_name = "The User" if use_default else None
    user = get_user_from_uid(uid)
    if not user:
        return default_name

    display_name = user.get("display_name") or default_name
    if display_name and display_name != "AnonymousUser":
        display_name = display_name.split(" ")[0]

    cache_user_name(uid, display_name, ttl=60 * 60)
    return display_name


def get_user_creation_time(uid: str) -> int | None:
    """Account creation time in epoch milliseconds (matches Firebase's
    user_metadata.creation_timestamp convention), sourced from Casdoor's
    createdTime. Returns None when unavailable — callers fail open."""
    if not uid:
        return None
    base = os.environ.get("CASDOOR_ENDPOINT", "").rstrip("/")
    if not base:
        return None
    try:
        params = _casdoor_params()
        params["id"] = uid
        resp = requests.get(f"{base}/api/get-user", params=params, timeout=5)
        resp.raise_for_status()
        user = (resp.json() or {}).get("data")
        created = user.get("createdTime") if user else None
        if not created:
            return None
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception as e:
        print(f"Error fetching creation time for {uid} from Casdoor: {e}")
        return None


def delete_account(uid: str):
    """Delete a user from Casdoor."""
    base = os.environ.get("CASDOOR_ENDPOINT", "").rstrip("/")
    if not base:
        return {"message": "Auth provider not configured"}

    # Parse org and username from sub format "org/username"
    parts = uid.split("/", 1)
    if len(parts) != 2:
        return {"message": f"Invalid uid format: {uid}"}
    owner, name = parts

    try:
        params = _casdoor_params()
        resp = requests.delete(
            f"{base}/api/delete-user",
            params=params,
            json={"owner": owner, "name": name},
            timeout=5,
        )
        resp.raise_for_status()
        return {"message": "User deleted"}
    except Exception as e:
        print(f"Error deleting user {uid} from Casdoor: {e}")
        return {"message": f"Failed to delete user: {e}"}
