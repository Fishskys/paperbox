"""MinIO object storage for paper originals and derived artifacts.

Object layout (plan section 6)::

    papers/<paper_id>/original.pdf
    papers/<paper_id>/extracted/text.json
    papers/<paper_id>/extracted/metadata.json
    papers/<paper_id>/figures/figure_001.png
    papers/<paper_id>/supplementary/...

The API never exposes MinIO URLs to Hermes: clients go through
``GET /api/papers/{paper_id}/file`` and this service streams the object.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO

from minio import Minio
from minio.datatypes import Object as MinioObject
from minio.error import S3Error

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ORIGINAL_FILENAME = "original.pdf"
ORIGINAL_PREFIX = "papers"
#: Prefix of the staging area used by ``POST /api/papers/ingest/files``.
UPLOAD_PREFIX = "uploads"
DEFAULT_PDF_CONTENT_TYPE = "application/pdf"

_client_holder: list[Minio] = []


class ObjectStorageError(RuntimeError):
    """Raised when MinIO cannot satisfy a request."""


class ObjectNotFound(ObjectStorageError):
    """Raised when the requested object key does not exist."""


@dataclass(frozen=True)
class StoredObject:
    """Result of an upload."""

    bucket: str
    object_key: str
    size_bytes: int
    content_type: str
    etag: str | None = None
    #: SHA256 of the uploaded bytes, when the caller streamed them through
    #: :func:`upload_stream_hashed` (``None`` for the in-memory helpers).
    sha256: str | None = None

    @property
    def path(self) -> str:
        return f"{self.bucket}/{self.object_key}"


class _HashingReader:
    """Binary stream wrapper that hashes every byte it hands out.

    MinIO pulls the payload out of this object, so the digest is ready by the
    time ``put_object`` returns -- no second pass, no full copy in memory. Only
    ``read``/``readinto`` are intercepted; everything else (``seekable``,
    ``tell``, ``close``, ...) is delegated to the wrapped stream.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = self._stream.read(-1 if size is None else size)
        if chunk:
            self._digest.update(chunk)
            self.bytes_read += len(chunk)
        return chunk

    def readinto(self, buffer) -> int:
        read = self._stream.readinto(buffer)
        if read:
            self._digest.update(memoryview(buffer)[:read])
            self.bytes_read += read
        return read

    @property
    def hexdigest(self) -> str:
        """SHA256 of everything read so far (final once the stream is drained)."""
        return self._digest.hexdigest()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def build_object_key(paper_id: str, filename: str = ORIGINAL_FILENAME) -> str:
    """Canonical object key for one paper artifact."""
    return f"{ORIGINAL_PREFIX}/{paper_id}/{filename}"


def build_extracted_key(paper_id: str, filename: str) -> str:
    return build_object_key(paper_id, f"extracted/{filename}")


def build_figure_key(paper_id: str, filename: str) -> str:
    return build_object_key(paper_id, f"figures/{filename}")


def build_client() -> Minio:
    """Create a MinIO client from settings (never hard-codes credentials)."""
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def get_client() -> Minio:
    """Process-wide MinIO client."""
    if not _client_holder:
        _client_holder.append(build_client())
    return _client_holder[0]


def ensure_bucket(bucket: str | None = None) -> str:
    """Create the bucket when missing and return its name."""
    name = bucket or settings.minio_bucket
    client = get_client()
    if not client.bucket_exists(name):
        client.make_bucket(name)
        logger.info("created MinIO bucket", extra={"extra_fields": {"bucket": name}})
    return name


def object_exists(object_key: str, bucket: str | None = None) -> bool:
    """True when the object key exists (no exception for missing keys)."""
    name = bucket or settings.minio_bucket
    try:
        get_client().stat_object(name, object_key)
        return True
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion"}:
            return False
        raise ObjectStorageError(f"stat_object failed for {name}/{object_key}") from exc


def upload_bytes(
    object_key: str,
    data: bytes,
    *,
    content_type: str = DEFAULT_PDF_CONTENT_TYPE,
    bucket: str | None = None,
    metadata: dict[str, str] | None = None,
) -> StoredObject:
    """Upload a byte payload under ``object_key``."""
    name = ensure_bucket(bucket)
    stream = io.BytesIO(data)
    try:
        result = get_client().put_object(
            name,
            object_key,
            stream,
            length=len(data),
            content_type=content_type,
            metadata=metadata,
        )
    except S3Error as exc:  # pragma: no cover - depends on live MinIO
        raise ObjectStorageError(
            f"put_object failed for {name}/{object_key}: {exc.code}"
        ) from exc
    logger.info(
        "uploaded object",
        extra={"extra_fields": {"bucket": name, "key": object_key, "bytes": len(data)}},
    )
    return StoredObject(
        bucket=name,
        object_key=object_key,
        size_bytes=len(data),
        content_type=content_type,
        etag=getattr(result, "etag", None),
    )


def upload_file(
    object_key: str,
    fileobj: BinaryIO,
    *,
    length: int,
    content_type: str = DEFAULT_PDF_CONTENT_TYPE,
    bucket: str | None = None,
    metadata: dict[str, str] | None = None,
) -> StoredObject:
    """Upload from an open binary stream of known ``length``."""
    name = ensure_bucket(bucket)
    try:
        result = get_client().put_object(
            name,
            object_key,
            fileobj,
            length=length,
            content_type=content_type,
            metadata=metadata,
        )
    except S3Error as exc:  # pragma: no cover - depends on live MinIO
        raise ObjectStorageError(
            f"put_object failed for {name}/{object_key}: {exc.code}"
        ) from exc
    logger.info(
        "uploaded stream",
        extra={"extra_fields": {"bucket": name, "key": object_key, "bytes": length}},
    )
    return StoredObject(
        bucket=name,
        object_key=object_key,
        size_bytes=length,
        content_type=content_type,
        etag=getattr(result, "etag", None),
    )


def upload_original_pdf(paper_id: str, data: bytes) -> StoredObject:
    """Store a paper PDF at ``papers/<paper_id>/original.pdf``."""
    return upload_bytes(
        build_object_key(paper_id),
        data,
        content_type=DEFAULT_PDF_CONTENT_TYPE,
        metadata={"paper_id": paper_id, "kind": "original"},
    )


def safe_filename(filename: str | None, default: str = ORIGINAL_FILENAME) -> str:
    """Reduce a client-supplied name to something safe inside an object key.

    Only the basename survives (no ``/``, no ``\\``, no ``..``), control
    characters and the characters that confuse shells or URLs are replaced by
    ``_``, and the result is truncated. The extension is *not* forced here: the
    caller decides the suffix it wants in the key.
    """
    raw = str(filename or "").strip().replace("\\", "/")
    base = raw.rsplit("/", 1)[-1].strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    cleaned = cleaned.replace("..", "_")
    if not cleaned:
        return default
    return cleaned[:120]


def build_staging_key(request_id: str, index: int, filename: str | None) -> str:
    """Staging object key for one part of an upload request.

    ``uploads/<request_id>/<index>-<safe-filename>.pdf`` -- the request id makes
    the group traceable, the index keeps the parts ordered and distinct even
    when the client repeats a filename.
    """
    stem = safe_filename(filename)
    if not stem.lower().endswith(".pdf"):
        stem = f"{stem}.pdf"
    safe_request = re.sub(r"[^A-Za-z0-9_-]+", "_", str(request_id))[:64] or "request"
    return f"{UPLOAD_PREFIX}/{safe_request}/{int(index)}-{stem}"


def upload_stream_hashed(
    object_key: str,
    fileobj: BinaryIO,
    *,
    length: int,
    content_type: str = DEFAULT_PDF_CONTENT_TYPE,
    bucket: str | None = None,
    metadata: dict[str, str] | None = None,
) -> StoredObject:
    """Upload from a stream and return the SHA256 of what was uploaded.

    The payload is never held in memory: MinIO pulls it out of a
    :class:`_HashingReader`, so the digest of a 100 MB PDF costs nothing extra.
    Used by the staging path of ``POST /api/papers/ingest/files``, which must
    dedupe by content but cannot read the body twice.
    """
    name = ensure_bucket(bucket)
    reader = _HashingReader(fileobj)
    try:
        result = get_client().put_object(
            name,
            object_key,
            reader,
            length=length,
            content_type=content_type,
            metadata=metadata,
        )
    except S3Error as exc:  # pragma: no cover - depends on live MinIO
        raise ObjectStorageError(
            f"put_object failed for {name}/{object_key}: {exc.code}"
        ) from exc
    digest = reader.hexdigest
    logger.info(
        "uploaded hashed stream",
        extra={
            "extra_fields": {
                "bucket": name,
                "key": object_key,
                "bytes": reader.bytes_read,
                "sha256": digest[:16],
            }
        },
    )
    return StoredObject(
        bucket=name,
        object_key=object_key,
        size_bytes=reader.bytes_read if reader.bytes_read else length,
        content_type=content_type,
        etag=getattr(result, "etag", None),
        sha256=digest,
    )


def download_bytes(object_key: str, bucket: str | None = None) -> bytes:
    """Download an object fully into memory."""
    name = bucket or settings.minio_bucket
    response = None
    try:
        response = get_client().get_object(name, object_key)
        return response.read()
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise ObjectNotFound(f"{name}/{object_key} not found") from exc
        raise ObjectStorageError(f"get_object failed for {name}/{object_key}") from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()


@contextmanager
def open_stream(object_key: str, bucket: str | None = None) -> Iterator[BinaryIO]:
    """Stream an object without buffering it in memory (for file downloads)."""
    name = bucket or settings.minio_bucket
    response = None
    try:
        response = get_client().get_object(name, object_key)
        yield response
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise ObjectNotFound(f"{name}/{object_key} not found") from exc
        raise ObjectStorageError(f"get_object failed for {name}/{object_key}") from exc
    finally:
        if response is not None:
            response.close()
            response.release_conn()


def stat_object(object_key: str, bucket: str | None = None) -> MinioObject:
    """Return object metadata (size, etag, content type, ...)."""
    name = bucket or settings.minio_bucket
    try:
        return get_client().stat_object(name, object_key)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise ObjectNotFound(f"{name}/{object_key} not found") from exc
        raise ObjectStorageError(f"stat_object failed for {name}/{object_key}") from exc


def delete_object(object_key: str, bucket: str | None = None) -> None:
    """Delete a single object (missing keys are ignored)."""
    name = bucket or settings.minio_bucket
    try:
        get_client().remove_object(name, object_key)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            return
        raise ObjectStorageError(f"remove_object failed for {name}/{object_key}") from exc
    logger.info(
        "deleted object", extra={"extra_fields": {"bucket": name, "key": object_key}}
    )


def delete_prefix(paper_id: str, bucket: str | None = None) -> int:
    """Delete every object under ``papers/<paper_id>/``; returns the count."""
    name = bucket or settings.minio_bucket
    prefix = f"{ORIGINAL_PREFIX}/{paper_id}/"
    removed = 0
    for obj in list_objects(prefix=prefix, bucket=name):
        delete_object(obj.object_name, bucket=name)
        removed += 1
    return removed


def list_objects(
    prefix: str = f"{ORIGINAL_PREFIX}/",
    bucket: str | None = None,
    *,
    recursive: bool = True,
) -> Iterable[MinioObject]:
    """List objects under ``prefix``."""
    name = bucket or settings.minio_bucket
    return get_client().list_objects(name, prefix=prefix, recursive=recursive)


def guess_content_type(filename: str) -> str:
    """Best-effort content type for a filename."""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def presigned_get_url(
    object_key: str, *, expires_seconds: int = 3600, bucket: str | None = None
) -> str:
    """Temporary URL, handy for internal debugging (not exposed via the API)."""
    from datetime import timedelta

    name = bucket or settings.minio_bucket
    return get_client().presigned_get_object(
        name, object_key, expires=timedelta(seconds=expires_seconds)
    )


def parse_last_modified(value: datetime | None) -> datetime | None:
    """Normalize MinIO timestamps (already tz-aware) for API responses."""
    return value


__all__ = [
    "DEFAULT_PDF_CONTENT_TYPE",
    "ORIGINAL_FILENAME",
    "UPLOAD_PREFIX",
    "ObjectNotFound",
    "ObjectStorageError",
    "StoredObject",
    "build_extracted_key",
    "build_figure_key",
    "build_object_key",
    "build_staging_key",
    "delete_object",
    "delete_prefix",
    "download_bytes",
    "ensure_bucket",
    "get_client",
    "guess_content_type",
    "list_objects",
    "object_exists",
    "open_stream",
    "presigned_get_url",
    "safe_filename",
    "stat_object",
    "upload_bytes",
    "upload_file",
    "upload_original_pdf",
    "upload_stream_hashed",
]
