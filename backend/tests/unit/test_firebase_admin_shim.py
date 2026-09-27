"""The self-hosted firebase_admin shim keeps the SDK contract upstream relies on. [fork-only]"""

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import firebase_admin
from firebase_admin import auth, credentials, messaging
from utils.firebase_admin_runtime import _AUTH_MUTATORS, install_firebase_auth_mutation_guard

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def casdoor_env(monkeypatch):
    monkeypatch.setenv("CASDOOR_ENDPOINT", "https://door.example.test")
    monkeypatch.setenv("CASDOOR_CLIENT_ID", "omi-client")
    monkeypatch.setenv("CASDOOR_CLIENT_SECRET", "secret")
    monkeypatch.delenv("CASDOOR_INTERNAL_URL", raising=False)
    monkeypatch.delenv("CASDOOR_ORGANIZATION", raising=False)


def _token(**claims):
    body = {"sub": "omi/alice", "aud": "omi-client", "exp": int(time.time()) + 300, **claims}
    return jwt.encode(body, _KEY, algorithm="RS256")


@pytest.fixture
def jwks():
    signing_key = MagicMock(key=_KEY.public_key())
    client = MagicMock()
    client.get_signing_key_from_jwt.return_value = signing_key
    with patch("utils.oidc._get_jwks_client", return_value=client):
        yield client


def _casdoor_response(data):
    resp = MagicMock()
    resp.json.return_value = {"status": "ok", "data": data}
    resp.raise_for_status.return_value = None
    return resp


class TestVerifyIdToken:
    def test_valid_token_exposes_sub_as_uid(self, jwks):
        claims = auth.verify_id_token(_token())
        assert claims["uid"] == "omi/alice"
        assert claims["sub"] == "omi/alice"

    def test_check_revoked_is_accepted(self, jwks):
        assert auth.verify_id_token(_token(), check_revoked=True)["uid"] == "omi/alice"

    def test_expired_token_raises_expired_error(self, jwks):
        with pytest.raises(auth.ExpiredIdTokenError):
            auth.verify_id_token(_token(exp=int(time.time()) - 10))

    def test_expired_is_an_invalid_token_error(self, jwks):
        # Upstream catches InvalidIdTokenError to cover expiry too.
        with pytest.raises(auth.InvalidIdTokenError):
            auth.verify_id_token(_token(exp=int(time.time()) - 10))

    def test_wrong_audience_is_invalid(self, jwks):
        with pytest.raises(auth.InvalidIdTokenError):
            auth.verify_id_token(_token(aud="someone-else"))

    def test_garbage_is_invalid(self, jwks):
        jwks.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("bad header")
        with pytest.raises(auth.InvalidIdTokenError):
            auth.verify_id_token("not-a-jwt")

    def test_key_fetch_failure_is_certificate_error(self, jwks):
        jwks.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError("down")
        with pytest.raises(auth.CertificateFetchError):
            auth.verify_id_token(_token())

    def test_unconfigured_casdoor_is_invalid_not_a_crash(self, monkeypatch):
        # Keeps upstream's LOCAL_DEVELOPMENT fallback reachable on a bare machine.
        monkeypatch.delenv("CASDOOR_ENDPOINT")
        with pytest.raises(auth.InvalidIdTokenError):
            auth.verify_id_token(_token())


class TestUserRecords:
    def test_get_user_maps_casdoor_fields(self):
        casdoor_user = {
            "owner": "omi",
            "name": "alice",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
            "phone": "+15550100",
            "avatar": "https://img/a.png",
            "createdTime": "2026-01-02T03:04:05Z",
        }
        with patch("firebase_admin._casdoor.requests.get", return_value=_casdoor_response(casdoor_user)) as get:
            user = auth.get_user("omi/alice")
        assert get.call_args.kwargs["params"]["id"] == "omi/alice"
        assert (user.uid, user.display_name, user.email, user.phone_number) == (
            "omi/alice",
            "Alice Smith",
            "alice@example.com",
            "+15550100",
        )
        assert user.disabled is False
        assert user.user_metadata.creation_timestamp == 1767323045000

    def test_bare_user_id_uses_user_id_lookup(self, monkeypatch):
        monkeypatch.setenv("CASDOOR_ORGANIZATION", "omi")
        with patch("firebase_admin._casdoor.requests.get", return_value=_casdoor_response({"name": "a"})) as get:
            auth.get_user("7c9e6679-7425-40de-944b-e07fc1f90ae7")
        params = get.call_args.kwargs["params"]
        assert params["userId"] == "7c9e6679-7425-40de-944b-e07fc1f90ae7"
        assert params["owner"] == "omi"
        assert "id" not in params

    def test_missing_user_raises_not_found(self):
        with patch("firebase_admin._casdoor.requests.get", return_value=_casdoor_response(None)):
            with pytest.raises(auth.UserNotFoundError):
                auth.get_user("omi/ghost")

    def test_delete_user_posts_owner_and_name(self):
        with patch(
            "firebase_admin._casdoor.requests.get",
            return_value=_casdoor_response({"owner": "omi", "name": "alice"}),
        ), patch("firebase_admin._casdoor.requests.post", return_value=_casdoor_response(True)) as post:
            auth.delete_user("omi/alice")
        assert post.call_args.kwargs["json"] == {"owner": "omi", "name": "alice"}

    def test_firebase_only_operations_are_explicitly_unsupported(self):
        with pytest.raises(auth.UnsupportedOperationError):
            auth.create_custom_token("omi/alice")


class TestSdkSurface:
    def test_initialize_app_is_idempotent(self):
        first = firebase_admin.initialize_app(credentials.Certificate({"project_id": "p"}))
        assert firebase_admin.initialize_app() is first
        assert firebase_admin.get_app() is first

    def test_upstream_mutation_guard_finds_every_name(self):
        # utils/firebase_admin_runtime.py refuses to start if any name is missing.
        assert all(hasattr(auth, name) for name in _AUTH_MUTATORS)
        stub = MagicMock(spec=list(_AUTH_MUTATORS))
        assert install_firebase_auth_mutation_guard({"OMI_JIT_QA_LOCAL_STACK": "1"}, auth_module=stub)

    def test_messaging_accepts_fields_the_stub_never_heard_of(self):
        message = messaging.Message(
            token="t",
            notification=messaging.Notification(title="hi", body="there", image="x"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(aps=messaging.Aps(alert=messaging.ApsAlert(title="a"), sound="default"))
            ),
            fcm_options=object(),
        )
        response = messaging.send_each([message, message])
        assert (response.success_count, response.failure_count) == (2, 0)
        assert all(r.success for r in response.responses)
        assert message.android is None
