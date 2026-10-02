"""MinIO storage shim: custom metadata and presign endpoint. [fork-only]

Needs MINIO_TEST_URL (e.g. http://localhost:9099 with minioadmin/minioadmin).
"""

import os
import uuid
from urllib.parse import urlparse

import pytest

TEST_URL = os.environ.get("MINIO_TEST_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="MINIO_TEST_URL not set")


@pytest.fixture
def bucket(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", TEST_URL)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "minioadmin")
    from utils.other.minio_storage import MinioStorageClient

    client = MinioStorageClient()
    name = f"shim-{uuid.uuid4().hex[:8]}"
    client._s3.create_bucket(Bucket=name)
    yield client.bucket(name)


def test_metadata_round_trip(bucket):
    blob = bucket.blob("cache/a.wav")
    blob.metadata = {"expires_at": "2026-10-05T00:00:00+00:00", "audio_file_id": "f1"}
    blob.upload_from_string(b"RIFF", content_type="audio/wav")
    fresh = bucket.blob("cache/a.wav")
    fresh.reload()
    assert fresh.metadata == {"expires_at": "2026-10-05T00:00:00+00:00", "audio_file_id": "f1"}


def test_no_metadata_is_none(bucket):
    bucket.blob("b").upload_from_string(b"x")
    fresh = bucket.blob("b")
    fresh.reload()
    assert fresh.metadata is None


def test_presigned_url_uses_public_endpoint(bucket, monkeypatch):
    monkeypatch.setenv("S3_PRESIGN_ENDPOINT_URL", "https://files.example.ts.net")
    url = bucket.blob("b").generate_signed_url(expiration=60)
    assert urlparse(url).netloc == "files.example.ts.net"
