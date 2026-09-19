"""Streaming uploads compute the SHA256 while the bytes go by (2026-09-19).

``/ingest/files`` has to answer "is this the same PDF we already have?" but the
payload may be 100 MB. Reading it into memory to hash it (what ``upload_bytes``
does, and what ``/ingest/file`` used to do) doubles the peak footprint of every
upload for no reason: the digest can be computed on the way to MinIO.

``_HashingReader`` wraps the client's stream and updates a ``hashlib`` object as
MinIO pulls bytes out of it, so ``upload_stream_hashed`` returns the usual
:class:`~app.services.object_storage.StoredObject` **plus** ``sha256``.
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest
from minio.error import S3Error

from app.services import object_storage


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class GuardedStream:
    """A source stream that refuses to be read in one big gulp.

    ``read()`` without a size (or with a size above ``MAX_READ``) is exactly the
    "buffer the whole upload" behaviour these tests forbid, so it raises.
    """

    MAX_READ = 1024 * 1024

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)
        self.payload = payload
        self.requests: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.requests.append(size)
        if size is None or size < 0 or size > self.MAX_READ:
            raise AssertionError(f"unbounded read({size!r}) -- payload was buffered")
        return self._buffer.read(size)

    @property
    def max_request(self) -> int:
        return max((size or 0) for size in self.requests) if self.requests else 0


class FakeMinio:
    """MinIO client stub that drains whatever stream it is handed."""

    def __init__(self, chunk: int = 64 * 1024) -> None:
        self.chunk = chunk
        self.received: object | None = None
        self.length: int | None = None
        self.content_type: str | None = None
        self.metadata: dict[str, str] | None = None
        self.key: str | None = None
        self.chunks: list[int] = []

    def bucket_exists(self, _name: str) -> bool:
        return True

    def put_object(
        self,
        _bucket: str,
        key: str,
        data,
        length=None,
        content_type=None,
        metadata=None,
    ):
        self.received = data
        self.key = key
        self.length = length
        self.content_type = content_type
        self.metadata = metadata
        while True:
            piece = data.read(self.chunk)
            if not piece:
                break
            self.chunks.append(len(piece))
        return SimpleNamespace(etag="deadbeef")


class FailingMinio(FakeMinio):
    def put_object(self, *args, **kwargs):  # noqa: D102 - stub
        raise S3Error(
            code="AccessDenied",
            message="nope",
            resource="paperbox/uploads/x.pdf",
            request_id="req",
            host_id="host",
            response=None,
        )


@pytest.fixture()
def fake_minio(monkeypatch):
    client = FakeMinio()
    monkeypatch.setattr(object_storage, "get_client", lambda: client)
    return client


# --------------------------------------------------------------------------- #
# _HashingReader
# --------------------------------------------------------------------------- #
def test_hashing_reader_hashes_what_it_serves():
    payload = b"%PDF-1.7 hello world" * 100
    reader = object_storage._HashingReader(io.BytesIO(payload))

    served = b""
    while True:
        piece = reader.read(37)
        if not piece:
            break
        served += piece

    assert served == payload
    assert reader.hexdigest == hashlib.sha256(payload).hexdigest()
    assert reader.bytes_read == len(payload)


def test_hashing_reader_reads_incrementally():
    payload = b"x" * 5000
    reader = object_storage._HashingReader(io.BytesIO(payload))

    reader.read(100)
    partial = reader.hexdigest

    reader.read(4900)

    assert partial == hashlib.sha256(b"x" * 100).hexdigest()
    assert reader.hexdigest == hashlib.sha256(payload).hexdigest()


def test_hashing_reader_supports_readinto():
    payload = b"%PDF-1.4 readinto path"
    reader = object_storage._HashingReader(io.BytesIO(payload))
    buffer = bytearray(len(payload))

    read = reader.readinto(buffer)

    assert bytes(buffer) == payload
    assert read == len(payload)
    assert reader.hexdigest == hashlib.sha256(payload).hexdigest()


def test_hashing_reader_delegates_other_attributes():
    """MinIO inspects the stream (``seekable``/``tell``); it must pass through."""
    reader = object_storage._HashingReader(io.BytesIO(b"abc"))

    assert reader.seekable() is True
    assert reader.tell() == 0


# --------------------------------------------------------------------------- #
# upload_stream_hashed
# --------------------------------------------------------------------------- #
def test_upload_stream_hashed_returns_the_digest_and_the_size(fake_minio):
    payload = b"%PDF-1.7" + b"body" * 4096
    stream = GuardedStream(payload)

    stored = object_storage.upload_stream_hashed(
        "uploads/req/0-a.pdf", stream, length=len(payload)
    )

    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert stored.object_key == "uploads/req/0-a.pdf"
    assert sum(fake_minio.chunks) == len(payload)


def test_upload_stream_hashed_never_buffers_the_payload(fake_minio):
    payload = b"%PDF-1.7" + b"y" * (3 * 1024 * 1024)
    stream = GuardedStream(payload)

    object_storage.upload_stream_hashed(
        "uploads/req/0-big.pdf", stream, length=len(payload)
    )

    # The source was read in bounded pieces and handed over untouched.
    assert stream.max_request <= GuardedStream.MAX_READ
    assert fake_minio.received is not None
    assert not isinstance(fake_minio.received, io.BytesIO)
    assert fake_minio.length == len(payload)


def test_upload_stream_hashed_passes_content_type_and_metadata(fake_minio):
    payload = b"%PDF-1.7 tiny"
    stored = object_storage.upload_stream_hashed(
        "uploads/req/0-c.pdf",
        io.BytesIO(payload),
        length=len(payload),
        content_type="application/pdf",
        metadata={"request_id": "req"},
    )

    assert fake_minio.content_type == "application/pdf"
    assert fake_minio.metadata == {"request_id": "req"}
    assert stored.content_type == "application/pdf"


def test_upload_stream_hashed_wraps_storage_failures(monkeypatch):
    monkeypatch.setattr(object_storage, "get_client", lambda: FailingMinio())

    with pytest.raises(object_storage.ObjectStorageError):
        object_storage.upload_stream_hashed(
            "uploads/req/0-d.pdf", io.BytesIO(b"%PDF-1.7"), length=8
        )


def test_upload_stream_hashed_matches_an_independent_hash(fake_minio):
    """Cross-check against a hash computed the ordinary way, chunk by chunk."""
    payload = bytes(range(256)) * 9000
    expected = hashlib.sha256()
    for offset in range(0, len(payload), 7919):
        expected.update(payload[offset : offset + 7919])

    stored = object_storage.upload_stream_hashed(
        "uploads/req/0-e.pdf", io.BytesIO(payload), length=len(payload)
    )

    assert stored.sha256 == expected.hexdigest()


def test_upload_bytes_leaves_sha256_unset():
    """The existing in-memory helper keeps its shape (sha256 is opt-in)."""
    assert object_storage.StoredObject(
        bucket="paperbox", object_key="k", size_bytes=1, content_type="application/pdf"
    ).sha256 is None
