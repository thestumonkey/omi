"""Tests for the Casdoor auth flow's redirect_uri handling in
``backend.routers.auth``:

* ``_validate_redirect_uri`` — server-side allowlist of redirect targets.
* ``auth_token`` — the auth code is bound to the redirect_uri it was issued
  with, and the exchange rejects a mismatch.

Real-world client redirect URIs this must keep accepting:
  omi://auth/callback             — Flutter mobile (app/lib/services/auth_service.dart)
  omi-computer://auth/callback    — desktop prod
  omi-computer-dev://auth/callback — desktop dev
  omi-<bundle>://auth/callback    — named desktop builds
  com.omi.app://auth/callback     — reverse-DNS form (RFC 8252)
  http://127.0.0.1:PORT/callback  — CLI loopback (also localhost / [::1])
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# Backend modules expect ENCRYPTION_SECRET / BASE_API_URL at import time.
os.environ.setdefault("ENCRYPTION_SECRET", "omi_test_secret_for_casdoor_redirect_unit_test_only")
os.environ.setdefault("BASE_API_URL", "http://localhost:8080")

# Pre-mock heavy deps before importing the module under test.
_mock = MagicMock()
for _m in ["firebase_admin.auth", "database.redis_db", "utils.http_client", "utils.log_sanitizer"]:
    sys.modules.setdefault(_m, _mock)

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from routers.auth import _validate_redirect_uri, auth_token  # noqa: E402

# ── Validator: accept every shape a real omi client sends ────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "omi://auth/callback",  # mobile
        "omi-computer://auth/callback",  # desktop prod
        "omi-computer-dev://auth/callback",  # desktop dev
        "omi-fix-rewind://auth/callback",  # named bundle (omi-{anything})
        "com.omi.app://auth/callback",  # reverse-DNS
        "http://127.0.0.1:8765/callback",  # CLI loopback (IPv4)
        "http://localhost:5000/callback",  # CLI loopback (hostname)
        "http://[::1]:5000/callback",  # CLI loopback (IPv6)
        "omi://",  # custom scheme, no path
        "omi-computer://auth/callback?from=settings",  # query preserved
    ],
)
def test_validator_accepts_known_client_shapes(uri: str) -> None:
    assert _validate_redirect_uri(uri) is True


# ── Validator: reject anything that could leak the code off-device ───────────


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "   ",
        "auth/callback",  # no scheme
        "https://attacker.example.com/cb",
        "https://localhost/cb",  # https never allowed, even loopback
        "https://127.0.0.1:5000/callback",  # RFC 8252 §7.3: loopback is http only
        "http://attacker.example.com/cb",  # non-loopback http
        "http://192.168.1.42/cb",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "about:blank",
        "://x",  # empty scheme
        "1omi://auth/callback",  # scheme must start with a letter
        "omi$://auth/callback",  # '$' not a valid scheme char
        "omicron://auth/callback",  # 'omi' must be followed by end or '-'
        "ômi://auth/callback",  # non-ASCII look-alike
        "оmi://auth/callback",  # cyrillic 'o'
    ],
)
def test_validator_rejects_dangerous_or_malformed(uri: str) -> None:
    assert _validate_redirect_uri(uri) is False


# ── Auth code binding: redirect_uri is bound to the code at /token ───────────


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bound_code(redirect_uri: str) -> str:
    return json.dumps({"tokens": {"id_token": "tok", "access_token": "at"}, "redirect_uri": redirect_uri})


class TestAuthCodeBinding:
    def test_token_rejects_redirect_uri_mismatch(self):
        with patch("routers.auth.get_auth_code", return_value=_bound_code("omi-computer://auth/callback")), patch(
            "routers.auth.delete_auth_code"
        ):
            with pytest.raises(HTTPException) as exc:
                _run(
                    auth_token(
                        request=MagicMock(),
                        grant_type="authorization_code",
                        code="c",
                        redirect_uri="omi-evil://auth/callback",
                    )
                )
            assert exc.value.status_code == 400
            assert "mismatch" in exc.value.detail.lower()

    def test_token_accepts_matching_redirect_uri(self):
        with patch("routers.auth.get_auth_code", return_value=_bound_code("omi://auth/callback")), patch(
            "routers.auth.delete_auth_code"
        ):
            result = _run(
                auth_token(
                    request=MagicMock(),
                    grant_type="authorization_code",
                    code="c",
                    redirect_uri="omi://auth/callback",
                )
            )
            assert result["id_token"] == "tok"
            assert result["token_type"] == "Bearer"

    def test_token_handles_legacy_format(self):
        """Codes minted before binding rolled out stored tokens directly; still redeemable."""
        legacy = json.dumps({"id_token": "legacy-tok", "access_token": "at"})
        with patch("routers.auth.get_auth_code", return_value=legacy), patch("routers.auth.delete_auth_code"):
            result = _run(
                auth_token(
                    request=MagicMock(),
                    grant_type="authorization_code",
                    code="c",
                    redirect_uri="omi://auth/callback",
                )
            )
            assert result["id_token"] == "legacy-tok"

    def test_token_is_single_use(self):
        with patch("routers.auth.get_auth_code", return_value=_bound_code("omi://auth/callback")), patch(
            "routers.auth.delete_auth_code"
        ) as mock_delete:
            _run(
                auth_token(
                    request=MagicMock(),
                    grant_type="authorization_code",
                    code="the-code",
                    redirect_uri="omi://auth/callback",
                )
            )
            mock_delete.assert_called_once_with("the-code")

    def test_token_rejects_expired_code(self):
        with patch("routers.auth.get_auth_code", return_value=None):
            with pytest.raises(HTTPException) as exc:
                _run(
                    auth_token(
                        request=MagicMock(),
                        grant_type="authorization_code",
                        code="gone",
                        redirect_uri="omi://auth/callback",
                    )
                )
            assert exc.value.status_code == 400
            assert "expired" in exc.value.detail.lower()

    def test_token_rejects_unsupported_grant_type(self):
        with pytest.raises(HTTPException) as exc:
            _run(
                auth_token(
                    request=MagicMock(),
                    grant_type="client_credentials",
                    code="c",
                    redirect_uri="omi://auth/callback",
                )
            )
        assert exc.value.status_code == 400
