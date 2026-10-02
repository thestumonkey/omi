"""Upstream desktop sign-in over Casdoor: PKCE, custom token, Firebase REST stand-in. [fork-only]"""

import base64
import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ENCRYPTION_SECRET", "omi_test_secret_for_casdoor_firebase_rest_unit_test")
os.environ.setdefault("BASE_API_URL", "http://localhost:8080")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from firebase_admin import _wrapped_token  # noqa: E402
from routers import casdoor_auth  # noqa: E402

VERIFIER = "a-long-random-pkce-verifier-string-0123456789abcdefghijklmnop"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
REDIRECT = "http://127.0.0.1:54321/callback"
TOKENS = {"id_token": "casdoor-id-token", "refresh_token": "casdoor-refresh", "expires_in": 7200}
CLAIMS = {"uid": "omi/alice", "sub": "omi/alice", "iat": 1700000000, "exp": 1700007200, "email": "a@x.io"}


def _payload(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@pytest.fixture
def client():
    store = {}
    app = FastAPI()
    app.include_router(casdoor_auth.router)
    app.include_router(casdoor_auth.firebase_rest_router)
    with patch.object(casdoor_auth, "set_auth_code", lambda k, v, ttl: store.__setitem__(k, v)), patch.object(
        casdoor_auth, "get_auth_code", lambda k: store.get(k)
    ), patch.object(casdoor_auth, "delete_auth_code", lambda k: store.pop(k, None)), patch.object(
        casdoor_auth.firebase_auth, "verify_id_token", lambda t: dict(CLAIMS)
    ):
        yield TestClient(app), store


def _issue_code(store, challenge=CHALLENGE):
    store["auth-code"] = json.dumps({"tokens": TOKENS, "redirect_uri": REDIRECT, "code_challenge": challenge})


def _token_request(tc, **overrides):
    form = {
        "grant_type": "authorization_code",
        "code": "auth-code",
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "use_custom_token": "true",
        **overrides,
    }
    return tc.post("/v1/auth/token", data=form)


class TestPkce:
    def test_matching_verifier_is_accepted(self, client):
        tc, store = client
        _issue_code(store)
        assert _token_request(tc).status_code == 200

    def test_wrong_verifier_is_rejected(self, client):
        tc, store = client
        _issue_code(store)
        r = _token_request(tc, code_verifier="someone-elses-verifier-0123456789abcdefghijklmnopq")
        assert r.status_code == 400 and "code_verifier" in r.json()["detail"]

    def test_missing_verifier_is_rejected_when_flow_used_pkce(self, client):
        tc, store = client
        _issue_code(store)
        form = {"grant_type": "authorization_code", "code": "auth-code", "redirect_uri": REDIRECT}
        assert tc.post("/v1/auth/token", data=form).status_code == 400

    def test_flow_without_pkce_still_works(self, client):
        # The phone app (Flutter) never sends a challenge.
        tc, store = client
        _issue_code(store, challenge=None)
        form = {"grant_type": "authorization_code", "code": "auth-code", "redirect_uri": REDIRECT}
        r = tc.post("/v1/auth/token", data=form)
        assert r.status_code == 200 and "custom_token" not in r.json()

    def test_authorize_rejects_plain_challenge_method(self, client):
        tc, _ = client
        r = tc.get(
            "/v1/auth/authorize",
            params={"redirect_uri": REDIRECT, "code_challenge": "x", "code_challenge_method": "plain"},
            follow_redirects=False,
        )
        assert r.status_code == 400


class TestCustomTokenRedemption:
    def test_custom_token_redeems_once_for_the_same_tokens(self, client):
        tc, store = client
        _issue_code(store)
        custom = _token_request(tc).json()["custom_token"]
        url = "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=anything"
        r = tc.post(url, json={"token": custom, "returnSecureToken": True})
        assert r.status_code == 200
        body = r.json()
        assert _wrapped_token.unwrap(body["idToken"]) == "casdoor-id-token"
        assert body["refreshToken"] == "casdoor-refresh"
        assert body["expiresIn"] == "7200"  # Firebase sends a string
        assert body["localId"] == "omi/alice"
        again = tc.post(url, json={"token": custom, "returnSecureToken": True})
        assert again.status_code == 400
        assert again.json()["error"]["message"] == "INVALID_CUSTOM_TOKEN"

    def test_unknown_token_is_rejected(self, client):
        tc, _ = client
        r = tc.post("/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken", json={"token": "forged"})
        assert r.status_code == 400


def _casdoor_reply(status, body):
    resp = MagicMock(status_code=status, headers={"content-type": "application/json"})
    resp.json.return_value = body
    return resp


class TestRefresh:
    URL = "/securetoken.googleapis.com/v1/token?key=anything"

    def _refresh(self, tc, reply):
        async def fake_post(_token):
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(casdoor_auth, "_post_refresh_token", fake_post):
            return tc.post(self.URL, data={"grant_type": "refresh_token", "refresh_token": "old"})

    def test_success_uses_firebase_shape(self, client):
        tc, _ = client
        r = self._refresh(
            tc, _casdoor_reply(200, {"id_token": "new-id", "refresh_token": "new-rt", "expires_in": 3600})
        )
        assert r.status_code == 200
        body = r.json()
        assert _wrapped_token.unwrap(body.pop("id_token")) == "new-id"
        assert _wrapped_token.unwrap(body.pop("access_token")) == "new-id"
        assert body == {
            "expires_in": "3600",
            "token_type": "Bearer",
            "refresh_token": "new-rt",
            "user_id": "omi/alice",
            "project_id": "selfhosted",
        }

    def test_phone_sdk_posts_json(self, client):
        tc, _ = client
        reply = _casdoor_reply(200, {"id_token": "new-id", "refresh_token": "new-rt", "expires_in": 3600})

        async def fake_post(token):
            assert token == "old"
            return reply

        with patch.object(casdoor_auth, "_post_refresh_token", fake_post):
            r = tc.post(self.URL, json={"grantType": "refresh_token", "refreshToken": "old"})
        assert r.status_code == 200
        assert _wrapped_token.unwrap(r.json()["access_token"]) == "new-id"

    def test_missing_refresh_token_is_rejected(self, client):
        tc, _ = client
        r = tc.post(self.URL, json={"grantType": "refresh_token"})
        assert r.status_code == 400

    def test_rejected_refresh_token_is_definitive(self, client):
        # Casdoor answers a bad refresh token with an OAuth error body.
        tc, _ = client
        r = self._refresh(tc, _casdoor_reply(200, {"error": "invalid_grant"}))
        assert r.status_code == 400
        assert r.json()["error"]["message"] == "INVALID_REFRESH_TOKEN"

    def test_casdoor_outage_is_not_definitive(self, client):
        tc, _ = client
        r = self._refresh(tc, ConnectionError("casdoor down"))
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "UNAVAILABLE"


class TestWrappedToken:
    def test_has_the_claims_the_phone_sdk_requires(self):
        payload = _payload(_wrapped_token.wrap("inner", CLAIMS))
        assert payload["auth_time"] == payload["iat"] == 1700000000
        assert payload["exp"] == 1700007200
        assert payload["firebase"]["sign_in_provider"] == "custom"
        assert payload["user_id"] == payload["sub"] == "omi/alice"

    def test_unwrap_returns_inner_token_and_leaves_others_alone(self):
        assert _wrapped_token.unwrap(_wrapped_token.wrap("inner", CLAIMS)) == "inner"
        assert _wrapped_token.unwrap("a.b.signature") == "a.b.signature"
        assert _wrapped_token.unwrap("not-a-jwt") == "not-a-jwt"

    def test_verify_id_token_checks_the_inner_token(self):
        from firebase_admin import auth as shim

        seen = []
        with patch("utils.oidc.verify_oidc_token", lambda t: seen.append(t) or {"sub": "omi/alice"}), patch.object(
            shim._casdoor, "configured", lambda: True
        ):
            claims = shim.verify_id_token(_wrapped_token.wrap("inner", CLAIMS))
        assert seen == ["inner"] and claims["uid"] == "omi/alice"


class TestAccount:
    def test_lookup_returns_the_signed_in_user(self, client):
        tc, _ = client
        r = tc.post("/identitytoolkit.googleapis.com/v1/accounts:lookup?key=k", json={"idToken": "t"})
        assert r.status_code == 200
        [user] = r.json()["users"]
        assert user["localId"] == "omi/alice" and user["email"] == "a@x.io"

    def test_update_echoes_the_new_name(self, client):
        tc, _ = client
        r = tc.post("/identitytoolkit.googleapis.com/v1/accounts:update", json={"idToken": "t", "displayName": "Al"})
        assert r.status_code == 200 and r.json()["displayName"] == "Al"

    def test_bad_token_is_rejected(self, client):
        tc, _ = client

        def reject(_t):
            raise casdoor_auth.firebase_auth.InvalidIdTokenError("bad")

        with patch.object(casdoor_auth.firebase_auth, "verify_id_token", reject):
            r = tc.post("/identitytoolkit.googleapis.com/v1/accounts:lookup", json={"idToken": "t"})
        assert r.status_code == 400


class TestIosSdkPaths:
    V3 = "/www.googleapis.com/identitytoolkit/v3/relyingparty"

    def test_v3_custom_token_and_account_paths(self, client):
        tc, store = client
        _issue_code(store)
        custom = _token_request(tc).json()["custom_token"]
        r = tc.post(f"{self.V3}/verifyCustomToken?key=k", json={"token": custom, "returnSecureToken": True})
        assert r.status_code == 200 and r.json()["localId"] == "omi/alice"
        r = tc.post(f"{self.V3}/getAccountInfo?key=k", json={"idToken": r.json()["idToken"]})
        assert r.status_code == 200 and r.json()["users"][0]["localId"] == "omi/alice"
        r = tc.post(f"{self.V3}/setAccountInfo?key=k", json={"idToken": "t", "displayName": "Al"})
        assert r.status_code == 200 and r.json()["displayName"] == "Al"
