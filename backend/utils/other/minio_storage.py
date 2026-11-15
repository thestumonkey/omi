"""
A google-cloud-storage-compatible client backed by S3/MinIO (boto3).

Lets upstream `backend/utils/other/storage.py` run UNCHANGED by swapping only
its module-level `storage_client = storage.Client()` for `MinioStorageClient()`.
All 37 `.bucket()` / 33 `.blob()` call sites flow through this adapter.

Implements the GCS surface storage.py uses:
  Client.bucket(name) -> Bucket
  Bucket.blob(name) -> Blob ; Bucket.list_blobs(prefix=) -> Iterable[Blob]
  Blob.upload_from_filename/upload_from_string/download_to_filename/
      download_as_bytes/delete/exists/reload/open('wb')/make_public/
      generate_signed_url ; Blob.name / Blob.size / Blob.public_url
  Generations (GCS object versions): Blob.generation / metageneration /
      updated, if_generation_match on reads and writes (PreconditionFailed),
      Bucket.blob(name, generation=N), Blob.patch / compose, ranged reads

Backend = any S3-compatible store (MinIO, AWS S3, Cloudflare R2). Config via env:
  S3_ENDPOINT_URL (e.g. http://minio:9000; omit for AWS S3)
  S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY (fall back to AWS_* names)
  S3_REGION (default us-east-1) ; S3_PUBLIC_URL_BASE (optional, for public_url)
  S3_PRESIGN_ENDPOINT_URL (optional) — address clients use for presigned URLs,
      when S3_ENDPOINT_URL is only reachable inside the cluster

Raises google.cloud.exceptions.NotFound on missing objects so storage.py's
existing `except NotFound` handlers keep working — that exception type needs no
GCP credentials and makes no network calls.
"""

import datetime
import hashlib
import io
import os
import time

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from google.api_core.exceptions import PreconditionFailed
from google.cloud.exceptions import NotFound


def _s3_client(endpoint_url=None):
    return boto3.client(
        's3',
        endpoint_url=endpoint_url or os.getenv('S3_ENDPOINT_URL') or None,
        aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID') or os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY') or os.getenv('AWS_SECRET_ACCESS_KEY'),
        region_name=os.getenv('S3_REGION', 'us-east-1'),
        config=Config(signature_version='s3v4'),
    )


def read_own_presigned_url(url: str) -> bytes:
    """Read one of our own presigned storage URLs, via the storage API.

    Presigned URLs are signed for S3_PRESIGN_ENDPOINT_URL, a tailnet name pods
    cannot resolve, so the server reads its own URLs with storage credentials
    instead of fetching them. Only URLs on our storage endpoints are accepted,
    so a caller-supplied URL can never make the server request an arbitrary
    address.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    own_hosts = {
        urlparse(endpoint).netloc
        for endpoint in (os.getenv('S3_PRESIGN_ENDPOINT_URL'), os.getenv('S3_ENDPOINT_URL'))
        if endpoint
    }
    if parsed.scheme not in ('http', 'https') or parsed.netloc not in own_hosts:
        raise ValueError('URL is not on this server\'s storage')
    bucket, _, key = unquote(parsed.path).lstrip('/').partition('/')
    if not bucket or not key:
        raise ValueError('storage URL has no bucket or key')
    return _s3_client().get_object(Bucket=bucket, Key=key)['Body'].read()


def _is_404(err: ClientError) -> bool:
    code = err.response.get('Error', {}).get('Code', '')
    return code in ('404', 'NoSuchKey', 'NotFound', 'NoSuchBucket')


class _BlobWriter:
    """File-like context manager for Blob.open('wb', ...) — buffers then uploads."""

    def __init__(self, blob: 'Blob', content_type=None):
        self._blob = blob
        self._content_type = content_type
        self._buf = io.BytesIO()

    def write(self, data):
        return self._buf.write(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._blob.upload_from_string(self._buf.getvalue(), content_type=self._content_type)
        finally:
            self._buf.close()
        return False


# GCS gives every object a generation (a new number on each write) and a
# metageneration (a new number on each metadata change). S3 has neither, so the
# shim keeps them in two hidden metadata keys that `Blob.metadata` never shows.
_GEN_KEY = 'omi-generation'
_METAGEN_KEY = 'omi-metageneration'
_HIDDEN = (_GEN_KEY, _METAGEN_KEY)


def _new_generation() -> int:
    # Microseconds, like GCS. Unique enough for one writer per object path.
    return time.time_ns() // 1000


def _etag_generation(etag: str) -> int:
    # Objects written before the shim kept a generation: derive a stable one.
    return int(hashlib.sha256((etag or '').strip('"').encode()).hexdigest()[:15], 16) or 1


def _precondition(name: str, wanted, actual) -> None:
    if wanted is not None and int(wanted) != int(actual or 0):
        raise PreconditionFailed(f'{name}: generation {actual} does not match {wanted}')


class _BlobReader:
    """File-like reader for Blob.open('rb')."""

    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        return self._body.read() if size is None or size < 0 else self._body.read(size)

    def close(self):
        self._body.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Blob:
    def __init__(self, client, bucket_name: str, name: str, generation=None):
        self._s3 = client
        self._bucket = bucket_name
        self.name = name
        self.size = None
        # Bucket.blob(name, generation=N) pins one version: reads fail with
        # NotFound once the object has a newer generation (no S3 versioning).
        self._pinned = int(generation) if generation is not None else None
        self._generation = None
        self._metageneration = None
        self.updated = None
        self.content_encoding = None
        self.cache_control = None
        self.content_type = None
        self.chunk_size = None
        # GCS custom metadata: set before an upload to store it; filled by reload().
        # A blob from list_blobs loads it on first read, like GCS listings carry it.
        self._metadata = None
        self._metadata_pending = False

    @property
    def bucket(self) -> 'Bucket':
        # GCS blobs carry their bucket; upstream reads blob.bucket.name (for
        # example database/account_deletion_operational_holds.py in audio merge).
        return Bucket(self._s3, self._bucket)

    # ---- object state ----
    def _apply_head(self, head: dict) -> None:
        raw = head.get('Metadata') or {}
        self.size = head.get('ContentLength', self.size)
        self.updated = head.get('LastModified')
        self.content_encoding = head.get('ContentEncoding') or None
        self.cache_control = head.get('CacheControl') or None
        self.content_type = head.get('ContentType') or None
        self._generation = int(raw[_GEN_KEY]) if raw.get(_GEN_KEY) else _etag_generation(head.get('ETag'))
        self._metageneration = int(raw.get(_METAGEN_KEY) or 1)
        self._metadata = {k: v for k, v in raw.items() if k not in _HIDDEN} or None
        self._metadata_pending = False
        if self._pinned is not None and self._pinned != self._generation:
            raise NotFound(f'{self.name}#{self._pinned}')

    def _head(self) -> dict:
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=self.name)
        except ClientError as e:
            if _is_404(e):
                raise NotFound(self.name)
            raise
        self._apply_head(head)
        return head

    def _lazy(self):
        if self._metadata_pending:
            try:
                self._head()
            except NotFound:
                self._metadata_pending = False

    @property
    def generation(self):
        self._lazy()
        return self._generation if self._generation is not None else self._pinned

    @property
    def metageneration(self):
        self._lazy()
        return self._metageneration

    @property
    def metadata(self):
        self._lazy()
        return self._metadata

    @metadata.setter
    def metadata(self, value):
        self._metadata_pending = False
        self._metadata = value

    def _write_args(self, generation: int, metageneration: int = 1) -> dict:
        meta = {str(k): str(v) for k, v in (self.metadata or {}).items() if v is not None and k not in _HIDDEN}
        meta[_GEN_KEY] = str(generation)
        meta[_METAGEN_KEY] = str(metageneration)
        args = {'Metadata': meta}
        if self.cache_control:
            args['CacheControl'] = self.cache_control
        if self.content_encoding:
            args['ContentEncoding'] = self.content_encoding
        return args

    def _check_write(self, if_generation_match) -> None:
        """if_generation_match=0 means create only; N means the current object is N."""
        if if_generation_match is None:
            return
        try:
            current = self._s3.head_object(Bucket=self._bucket, Key=self.name)
        except ClientError as e:
            if not _is_404(e):
                raise
            if int(if_generation_match) != 0:
                raise PreconditionFailed(f'{self.name}: object does not exist')
            return
        raw = current.get('Metadata') or {}
        actual = int(raw[_GEN_KEY]) if raw.get(_GEN_KEY) else _etag_generation(current.get('ETag'))
        _precondition(self.name, if_generation_match, actual)

    def _put(self, body, content_type=None, if_generation_match=None) -> None:
        self._check_write(if_generation_match)
        generation = _new_generation()
        kwargs = {'Bucket': self._bucket, 'Key': self.name, 'Body': body, **self._write_args(generation)}
        if content_type:
            kwargs['ContentType'] = content_type
        self._s3.put_object(**kwargs)
        self._generation, self._metageneration = generation, 1
        self.size = len(body) if isinstance(body, (bytes, bytearray)) else self.size

    # ---- uploads ----
    def upload_from_filename(self, file_path: str, content_type=None, *_, if_generation_match=None, **__):
        with open(file_path, 'rb') as f:
            self._put(f.read(), content_type, if_generation_match)

    def upload_from_string(self, data, content_type=None, *_, if_generation_match=None, **__):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self._put(data, content_type, if_generation_match)

    def upload_from_file(self, file_obj, content_type=None, *_, if_generation_match=None, **__):
        self._put(file_obj.read(), content_type, if_generation_match)

    def open(self, mode='wb', content_type=None, **_):
        if 'r' in mode:
            return _BlobReader(self._get()['Body'])
        return _BlobWriter(self, content_type)

    # ---- downloads ----
    def _get(self, if_generation_match=None, start=None, end=None) -> dict:
        kwargs = {'Bucket': self._bucket, 'Key': self.name}
        if start is not None or end is not None:
            # GCS `end` is inclusive, like an HTTP Range.
            kwargs['Range'] = f'bytes={start or 0}-{"" if end is None else end}'
        try:
            obj = self._s3.get_object(**kwargs)
        except ClientError as e:
            if _is_404(e):
                raise NotFound(self.name)
            raise
        self._apply_head(obj)
        _precondition(self.name, if_generation_match, self._generation)
        return obj

    def download_as_bytes(self, *_, start=None, end=None, if_generation_match=None, **__) -> bytes:
        return self._get(if_generation_match, start, end)['Body'].read()

    def download_to_filename(self, file_path: str, *_, if_generation_match=None, **__):
        data = self.download_as_bytes(if_generation_match=if_generation_match)
        with open(file_path, 'wb') as f:
            f.write(data)

    def download_to_file(self, file_obj, *_, if_generation_match=None, **__):
        file_obj.write(self.download_as_bytes(if_generation_match=if_generation_match))

    # ---- metadata / lifecycle ----
    def exists(self, *_, **__) -> bool:
        try:
            self._head()
            return True
        except NotFound:
            return False

    def reload(self, *_, **__):
        self._head()

    def patch(self, *_, if_generation_match=None, if_metageneration_match=None, **__):
        """Write metadata/cache_control without changing the bytes or the generation."""
        wanted = self._metadata
        cache_control = self.cache_control
        head = self._head()
        _precondition(self.name, if_generation_match, self._generation)
        if if_metageneration_match is not None and int(if_metageneration_match) != self._metageneration:
            raise PreconditionFailed(f'{self.name}: metageneration changed')
        self._metadata, self.cache_control = wanted, cache_control
        metageneration = self._metageneration + 1
        args = self._write_args(self._generation, metageneration)
        self._s3.copy_object(
            Bucket=self._bucket,
            Key=self.name,
            CopySource={'Bucket': self._bucket, 'Key': self.name},
            MetadataDirective='REPLACE',
            ContentType=head.get('ContentType') or 'application/octet-stream',
            **args,
        )
        self._metageneration = metageneration

    def delete(self, *_, if_generation_match=None, **__):
        self._check_write(if_generation_match)
        self._s3.delete_object(Bucket=self._bucket, Key=self.name)

    def compose(self, sources, *_, if_generation_match=None, **__):
        """Join sources into this object. S3 has no small-part compose: copy the bytes."""
        data = b''.join(source.download_as_bytes(if_generation_match=source._pinned) for source in sources)
        self._put(data, self.content_type, if_generation_match)

    def make_public(self, *_, **__):
        self._s3.put_object_acl(Bucket=self._bucket, Key=self.name, ACL='public-read')

    @property
    def public_url(self) -> str:
        base = os.getenv('S3_PUBLIC_URL_BASE') or os.getenv('S3_ENDPOINT_URL', '')
        return f"{base.rstrip('/')}/{self._bucket}/{self.name}"

    def generate_signed_url(self, expiration=None, version=None, method='GET', **_) -> str:
        if isinstance(expiration, datetime.timedelta):
            expires = int(expiration.total_seconds())
        elif isinstance(expiration, (int, float)):
            expires = int(expiration)
        else:
            expires = 3600
        op = 'get_object' if str(method).upper() == 'GET' else 'put_object'
        # The signature covers the host, so sign with the address the client will use.
        public_endpoint = os.getenv('S3_PRESIGN_ENDPOINT_URL')
        signer = _s3_client(public_endpoint) if public_endpoint else self._s3
        return signer.generate_presigned_url(op, Params={'Bucket': self._bucket, 'Key': self.name}, ExpiresIn=expires)


class Bucket:
    def __init__(self, client, name: str):
        self._s3 = client
        self.name = name

    def blob(self, name: str, *_, generation=None, **__) -> Blob:
        return Blob(self._s3, self.name, name, generation=generation)

    def get_blob(self, name: str, *_, **__):
        blob = self.blob(name)
        try:
            blob.reload()
        except NotFound:
            return None
        return blob

    def list_blobs(self, prefix: str = None, **_):
        paginator = self._s3.get_paginator('list_objects_v2')
        kwargs = {'Bucket': self.name}
        if prefix is not None:
            kwargs['Prefix'] = prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get('Contents', []):
                b = Blob(self._s3, self.name, obj['Key'])
                b.size = obj.get('Size')
                b.updated = obj.get('LastModified')
                b._metadata_pending = True
                yield b

    def copy_blob(self, blob: Blob, destination_bucket: 'Bucket', new_name: str = None, *_, **__) -> Blob:
        # A copy is a new write in GCS, so it gets its own generation.
        source = self.blob(blob.name)
        head = source._head()
        target = destination_bucket.blob(new_name or blob.name)
        target._metadata, target.cache_control = source._metadata, source.cache_control
        generation = _new_generation()
        self._s3.copy_object(
            Bucket=destination_bucket.name,
            Key=target.name,
            CopySource={'Bucket': self.name, 'Key': blob.name},
            MetadataDirective='REPLACE',
            ContentType=head.get('ContentType') or 'application/octet-stream',
            **target._write_args(generation),
        )
        target._generation, target._metageneration = generation, 1
        return target


class MinioStorageClient:
    """Drop-in replacement for google.cloud.storage.Client(), backed by S3/MinIO."""

    def __init__(self):
        self._s3 = _s3_client()

    def bucket(self, name: str) -> Bucket:
        return Bucket(self._s3, name)

    def get_bucket(self, name: str, *_, **__) -> Bucket:
        return Bucket(self._s3, name)

    def blob(self, bucket_name: str, blob_name: str) -> Blob:
        return Bucket(self._s3, bucket_name).blob(blob_name)
