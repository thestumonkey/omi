"""MinIO Blob carries its bucket, like a GCS blob. [fork-only]

utils/other/playback_cache.py writes a manifest with blob.bucket.blob(...).
Without Blob.bucket every audio merge failed with
"'Blob' object has no attribute 'bucket'". Needs no MinIO.
"""

from utils.other.minio_storage import Blob, Bucket


def test_blob_bucket_is_its_bucket():
    client = object()
    blob = Bucket(client, "omi-sync").blob("playback/u1/c1/conversation.mp3")
    assert isinstance(blob.bucket, Bucket)
    assert blob.bucket.name == "omi-sync"
    manifest = blob.bucket.blob(f"{blob.name}.manifest.json")
    assert isinstance(manifest, Blob)
    assert manifest.name == "playback/u1/c1/conversation.mp3.manifest.json"
    assert manifest._s3 is client
