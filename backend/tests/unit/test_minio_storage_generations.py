"""MinIO storage shim: GCS generations and preconditions. [fork-only]

Upstream's audio custody (utils/other/audio_outbox.py) reads blob.generation
and pins reads and deletes to it. Without it the pusher could not save live
audio, and phone conversations stayed "in progress" forever.

Needs MINIO_TEST_URL (e.g. http://localhost:9099 with minioadmin/minioadmin).
"""

import io
import os
import uuid

import pytest
from google.api_core.exceptions import PreconditionFailed
from google.cloud.exceptions import NotFound

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


def _reloaded(bucket, name, **kwargs):
    blob = bucket.blob(name, **kwargs)
    blob.reload()
    return blob


def test_each_write_gets_a_new_generation_and_metadata_stays_clean(bucket):
    blob = bucket.blob("a")
    blob.metadata = {"sha256": "x"}
    blob.upload_from_string(b"one")
    first = _reloaded(bucket, "a")
    assert first.generation and first.metadata == {"sha256": "x"}
    assert first.updated is not None and first.metageneration == 1
    bucket.blob("a").upload_from_string(b"two")
    assert _reloaded(bucket, "a").generation != first.generation


def test_create_only_and_generation_match(bucket):
    bucket.blob("a").upload_from_string(b"one", if_generation_match=0)
    with pytest.raises(PreconditionFailed):
        bucket.blob("a").upload_from_string(b"again", if_generation_match=0)
    gen = _reloaded(bucket, "a").generation
    assert bucket.blob("a").download_as_bytes(if_generation_match=gen) == b"one"
    with pytest.raises(PreconditionFailed):
        bucket.blob("a").download_as_bytes(if_generation_match=gen + 1)
    with pytest.raises(PreconditionFailed):
        bucket.blob("a").delete(if_generation_match=gen + 1)
    bucket.blob("a").delete(if_generation_match=gen)
    assert not bucket.blob("a").exists()


def test_pinned_generation_is_gone_after_overwrite(bucket):
    bucket.blob("a").upload_from_string(b"one")
    gen = _reloaded(bucket, "a").generation
    assert bucket.blob("a", generation=gen).download_as_bytes() == b"one"
    bucket.blob("a").upload_from_string(b"two")
    with pytest.raises(NotFound):
        bucket.blob("a", generation=gen).download_as_bytes()


def test_patch_keeps_generation_and_bumps_metageneration(bucket):
    bucket.blob("a").upload_from_string(b"one", content_type="audio/wav")
    blob = _reloaded(bucket, "a")
    gen = blob.generation
    blob.metadata = {"conversation_id": "c1"}
    blob.patch(if_generation_match=gen)
    after = _reloaded(bucket, "a")
    assert (after.generation, after.metageneration) == (gen, 2)
    assert after.metadata == {"conversation_id": "c1"} and after.content_type == "audio/wav"
    with pytest.raises(PreconditionFailed):
        after.patch(if_metageneration_match=1)


def test_ranges_streams_files_and_compose(bucket):
    bucket.blob("p1").upload_from_file(io.BytesIO(b"hello "))
    bucket.blob("p2").upload_from_string(b"world")
    assert bucket.blob("p1").download_as_bytes(start=0, end=3) == b"hell"
    parts = [bucket.blob(n, generation=_reloaded(bucket, n).generation) for n in ("p1", "p2")]
    bucket.blob("all").compose(parts, if_generation_match=0)
    with bucket.blob("all").open("rb", chunk_size=4) as source:
        assert source.read(4) + source.read() == b"hello world"
    out = io.BytesIO()
    bucket.blob("all").download_to_file(out)
    assert out.getvalue() == b"hello world"
    assert bucket.get_blob("missing") is None


def test_listed_and_copied_blobs_carry_generations(bucket):
    bucket.blob("src").upload_from_string(b"x")
    listed = next(iter(bucket.list_blobs(prefix="src")))
    assert listed.generation == _reloaded(bucket, "src").generation
    copy = bucket.copy_blob(listed, bucket, "dst")
    assert copy.generation != listed.generation and _reloaded(bucket, "dst").generation == copy.generation
