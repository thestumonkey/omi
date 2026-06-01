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

Backend = any S3-compatible store (MinIO, AWS S3, Cloudflare R2). Config via env:
  S3_ENDPOINT_URL (e.g. http://minio:9000; omit for AWS S3)
  S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY (fall back to AWS_* names)
  S3_REGION (default us-east-1) ; S3_PUBLIC_URL_BASE (optional, for public_url)

Raises google.cloud.exceptions.NotFound on missing objects so storage.py's
existing `except NotFound` handlers keep working — that exception type needs no
GCP credentials and makes no network calls.
"""

import datetime
import io
import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from google.cloud.exceptions import NotFound


def _s3_client():
    return boto3.client(
        's3',
        endpoint_url=os.getenv('S3_ENDPOINT_URL') or None,
        aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID') or os.getenv('AWS_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY') or os.getenv('AWS_SECRET_ACCESS_KEY'),
        region_name=os.getenv('S3_REGION', 'us-east-1'),
        config=Config(signature_version='s3v4'),
    )


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


class Blob:
    def __init__(self, client, bucket_name: str, name: str):
        self._s3 = client
        self._bucket = bucket_name
        self.name = name
        self.size = None

    # ---- uploads ----
    def upload_from_filename(self, file_path: str, content_type=None):
        extra = {'ContentType': content_type} if content_type else None
        self._s3.upload_file(file_path, self._bucket, self.name, ExtraArgs=extra)

    def upload_from_string(self, data, content_type=None):
        if isinstance(data, str):
            data = data.encode('utf-8')
        kwargs = {'Bucket': self._bucket, 'Key': self.name, 'Body': data}
        if content_type:
            kwargs['ContentType'] = content_type
        self._s3.put_object(**kwargs)

    def open(self, mode='wb', content_type=None, **_):
        if 'w' not in mode:
            raise NotImplementedError("Blob.open only supports write modes in this adapter")
        return _BlobWriter(self, content_type)

    # ---- downloads ----
    def download_to_filename(self, file_path: str):
        try:
            self._s3.download_file(self._bucket, self.name, file_path)
        except ClientError as e:
            if _is_404(e):
                raise NotFound(self.name)
            raise

    def download_as_bytes(self) -> bytes:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=self.name)['Body'].read()
        except ClientError as e:
            if _is_404(e):
                raise NotFound(self.name)
            raise

    # ---- metadata / lifecycle ----
    def exists(self) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self.name)
            return True
        except ClientError as e:
            if _is_404(e):
                return False
            raise

    def reload(self):
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=self.name)
            self.size = head.get('ContentLength')
        except ClientError as e:
            if _is_404(e):
                raise NotFound(self.name)
            raise

    def delete(self):
        self._s3.delete_object(Bucket=self._bucket, Key=self.name)

    def make_public(self):
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
        return self._s3.generate_presigned_url(op, Params={'Bucket': self._bucket, 'Key': self.name}, ExpiresIn=expires)


class Bucket:
    def __init__(self, client, name: str):
        self._s3 = client
        self.name = name

    def blob(self, name: str) -> Blob:
        return Blob(self._s3, self.name, name)

    def list_blobs(self, prefix: str = None, **_):
        paginator = self._s3.get_paginator('list_objects_v2')
        kwargs = {'Bucket': self.name}
        if prefix is not None:
            kwargs['Prefix'] = prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get('Contents', []):
                b = Blob(self._s3, self.name, obj['Key'])
                b.size = obj.get('Size')
                yield b


class MinioStorageClient:
    """Drop-in replacement for google.cloud.storage.Client(), backed by S3/MinIO."""

    def __init__(self):
        self._s3 = _s3_client()

    def bucket(self, name: str) -> Bucket:
        return Bucket(self._s3, name)

    def blob(self, bucket_name: str, blob_name: str) -> Blob:
        return Bucket(self._s3, bucket_name).blob(blob_name)
