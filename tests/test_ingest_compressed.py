"""``POST /api/papers/ingest/compressed``: a ZIP as the upload unit (2026-09-19).

Unpacking a client-supplied archive is the most dangerous thing these endpoints
do, so this file covers the guards as much as the happy path:

* zip-slip -- ``../evil.pdf``, an absolute name, a symlink entry and a device
  entry are never written, and nothing appears outside the extraction directory;
* zip bomb -- entry count, total uncompressed size and compression ratio are all
  refused with ``422`` *before* anything is unpacked;
* nested archives are counted as ignored, not unpacked;
* anything that is not a zip (a 7z file included) is refused with ``415``.

The jobs created here are ``local_path`` + ``cleanup_after=true``: the extracted
file and its directory disappear as the pipeline stores them.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import ingestion as ingest_api
from app.core.config import settings
from app.core.security import require_api_key, require_write
from app.db.session import get_db
from app.main import app
from app.services import upload_admission
from tests.test_ingest_files import QueueRecorder  # noqa: F401 - fixture helper
from tests.test_local_source import factory, make_paper_with_sha256  # noqa: F401

PDF_A = b"%PDF-1.7\n" + b"archive payload a\n" * 30
PDF_B = b"%PDF-1.7\n" + b"archive payload b\n" * 30
NOT_A_PDF = b"just some text"
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c\x00\x04"


# --------------------------------------------------------------------------- #
# archive builders
# --------------------------------------------------------------------------- #
def build_zip(entries: list[tuple[str, bytes]], *, mode: int | None = None) -> bytes:
    """A zip in memory; ``mode`` (e.g. ``0o120777``) marks every entry as that."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode is not None:
                info.create_system = 3  # unix: external_attr carries the mode
                info.external_attr = mode << 16
            else:
                info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def build_bomb(*, entries: int = 2, payload: bytes = b"a" * (200 * 1024)) -> bytes:
    return build_zip([(f"bomb-{index}.pdf", PDF_A + payload) for index in range(entries)])


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def tmp_archive_dir(tmp_path, monkeypatch):
    """Point the archive service at a per-test temp directory."""
    target = tmp_path / "archive-tmp"
    target.mkdir()
    monkeypatch.setattr(settings, "ingest_archive_tmp_dir", str(target))
    return target


@pytest.fixture()
def queued(monkeypatch):
    recorder = QueueRecorder()
    monkeypatch.setattr(ingest_api.job_queue, "submit", recorder)
    return recorder


@pytest.fixture()
def client(factory, queued, tmp_archive_dir, monkeypatch):  # noqa: F811
    gate = upload_admission.UploadAdmission(limit=4, depth_provider=lambda: 0)
    monkeypatch.setattr(ingest_api.upload_admission, "get_admission", lambda: gate)

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    app.dependency_overrides[require_write] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def post_archive(client, payload: bytes, name: str = "papers.zip"):
    return client.post(
        "/api/papers/ingest/compressed",
        files={"file": (name, payload, "application/zip")},
    )


def extraction_dirs(tmp_archive_dir) -> list[Path]:
    return [
        entry
        for entry in tmp_archive_dir.iterdir()
        if entry.is_dir() and entry.name.startswith("paperbox-")
    ]


def leftover_files(tmp_archive_dir) -> list[Path]:
    return [entry for entry in tmp_archive_dir.iterdir() if entry.is_file()]


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_zip_pdfs_are_unpacked_and_queued(client, tmp_archive_dir, queued, factory):  # noqa: F811
    response = post_archive(
        client,
        build_zip([("one.pdf", PDF_A), ("sub/two.pdf", PDF_B), ("notes.txt", NOT_A_PDF)]),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["entries_total"] == 3
    assert body["entries_ignored"] == 1
    assert body["accepted"] == 2
    assert (body["duplicate"], body["rejected"]) == (0, 0)

    names = sorted(entry["entry"] for entry in body["results"])
    assert names == ["one.pdf", "sub/two.pdf"]

    # The archive itself is gone; the extracted PDFs are still there for the
    # pipeline to pick up.
    assert leftover_files(tmp_archive_dir) == []
    dirs = extraction_dirs(tmp_archive_dir)
    assert len(dirs) == 1
    assert sorted(path.name for path in dirs[0].rglob("*.pdf")) == ["one.pdf", "two.pdf"]

    payloads = []
    session = factory()
    try:
        from app.db.models import IngestionJob

        for entry in body["results"]:
            payloads.append(dict(session.get(IngestionJob, entry["job_id"]).payload))
    finally:
        session.close()
    assert all(item["source_type"] == "local_path" for item in payloads)
    assert all(item["cleanup_after"] is True for item in payloads)
    assert all(Path(item["local_path"]).is_file() for item in payloads)
    # A folder-sized import is batch work.
    assert [priority for _, _, priority in queued.submitted] == [1, 1]


def test_an_empty_zip_is_accepted_with_zeroes(client, tmp_archive_dir):
    response = post_archive(client, build_zip([]))

    assert response.status_code == 202
    body = response.json()
    assert (body["entries_total"], body["accepted"], body["rejected"]) == (0, 0, 0)
    assert leftover_files(tmp_archive_dir) == []


def test_content_already_in_the_library_is_a_duplicate(client, factory, tmp_archive_dir):  # noqa: F811
    import hashlib

    existing_id = make_paper_with_sha256(factory, hashlib.sha256(PDF_A).hexdigest())

    body = post_archive(client, build_zip([("dup.pdf", PDF_A)])).json()

    assert (body["accepted"], body["duplicate"]) == (0, 1)
    item = body["results"][0]
    assert item["paper_id"] == existing_id
    assert item["job_id"] is None
    # The extracted copy is deleted immediately, and so is the emptied directory.
    assert extraction_dirs(tmp_archive_dir) == []


def test_an_entry_without_pdf_magic_is_rejected(client, tmp_archive_dir):
    body = post_archive(client, build_zip([("fake.pdf", NOT_A_PDF)])).json()

    assert body["rejected"] == 1
    assert body["results"][0]["error_code"] == "UNSUPPORTED_TYPE"
    assert extraction_dirs(tmp_archive_dir) == []


# --------------------------------------------------------------------------- #
# zip-slip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    ["../evil.pdf", "sub/../../evil.pdf", "/abs/evil.pdf", "C:/windows/evil.pdf"],
)
def test_traversal_and_absolute_entries_are_refused(client, tmp_archive_dir, tmp_path, name):
    body = post_archive(client, build_zip([(name, PDF_A), ("ok.pdf", PDF_B)])).json()

    assert body["entries_rejected"] == 1
    assert body["accepted"] == 1
    # Nothing escaped the extraction directory.
    assert not (tmp_path / "evil.pdf").exists()
    dirs = extraction_dirs(tmp_archive_dir)
    assert len(dirs) == 1
    written = {path.name for path in dirs[0].rglob("*") if path.is_file()}
    assert written == {"ok.pdf"}


def test_a_symlink_entry_is_refused(client, tmp_archive_dir):
    body = post_archive(
        client,
        build_zip([("link.pdf", PDF_A)], mode=0o120777),
    ).json()

    assert body["entries_rejected"] == 1
    assert body["accepted"] == 0
    dirs = extraction_dirs(tmp_archive_dir)
    assert dirs == [] or not any(path.is_file() for path in dirs[0].rglob("*"))


def test_a_device_entry_is_refused(client, tmp_archive_dir):
    body = post_archive(
        client,
        build_zip([("dev.pdf", PDF_A)], mode=0o020666),
    ).json()

    assert body["entries_rejected"] == 1
    assert body["accepted"] == 0


def test_a_nested_archive_is_ignored_not_unpacked(client, tmp_archive_dir):
    nested = build_zip([("inner.pdf", PDF_A)])

    body = post_archive(
        client,
        build_zip([("inner.zip", nested), ("ok.pdf", PDF_B)]),
    ).json()

    assert body["entries_ignored"] == 1
    assert body["accepted"] == 1
    dirs = extraction_dirs(tmp_archive_dir)
    written = {path.name for path in dirs[0].rglob("*") if path.is_file()}
    assert written == {"ok.pdf"}


# --------------------------------------------------------------------------- #
# zip bomb
# --------------------------------------------------------------------------- #
def test_compression_ratio_over_the_limit_is_a_422(client, tmp_archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "ingest_archive_max_ratio", 2)

    response = post_archive(client, build_bomb())

    assert response.status_code == 422
    assert "compression ratio" in response.json()["detail"]
    assert leftover_files(tmp_archive_dir) == []
    assert extraction_dirs(tmp_archive_dir) == []


def test_too_many_entries_is_a_422(client, tmp_archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "ingest_archive_max_files", 1)

    response = post_archive(client, build_zip([("a.pdf", PDF_A), ("b.pdf", PDF_B)]))

    assert response.status_code == 422
    assert "INGEST_ARCHIVE_MAX_FILES" in response.json()["detail"]
    assert extraction_dirs(tmp_archive_dir) == []


def test_total_uncompressed_size_over_the_limit_is_a_422(client, tmp_archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "ingest_archive_max_uncompressed_mb", 0)

    response = post_archive(client, build_bomb())

    assert response.status_code == 422
    assert "INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB" in response.json()["detail"]
    assert extraction_dirs(tmp_archive_dir) == []


def test_the_archive_itself_over_the_limit_is_a_422(client, tmp_archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "ingest_archive_max_mb", 0)

    response = post_archive(client, build_zip([("a.pdf", PDF_A)]))

    assert response.status_code == 422
    assert "INGEST_ARCHIVE_MAX_MB" in response.json()["detail"]
    assert leftover_files(tmp_archive_dir) == []
    assert extraction_dirs(tmp_archive_dir) == []


def test_an_entry_over_the_per_file_limit_is_rejected(client, tmp_archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "ingest_max_file_mb", 0)

    body = post_archive(client, build_zip([("big.pdf", PDF_A), ("ok.pdf", PDF_B)])).json()

    # Refused while unpacking, so they show up as rejected entries -- not as
    # unpacked PDFs that failed later.
    assert body["entries_rejected"] == 2
    assert body["accepted"] == 0
    assert extraction_dirs(tmp_archive_dir) == []


# --------------------------------------------------------------------------- #
# format gate
# --------------------------------------------------------------------------- #
def test_a_7z_upload_is_refused_with_415(client, tmp_archive_dir):
    response = post_archive(client, SEVEN_ZIP_MAGIC + b"\x00" * 64, name="papers.7z")

    assert response.status_code == 415
    assert "zip" in response.json()["detail"].lower()
    assert leftover_files(tmp_archive_dir) == []
    assert extraction_dirs(tmp_archive_dir) == []


def test_a_plain_text_upload_is_refused_with_415(client, tmp_archive_dir):
    response = post_archive(client, b"not an archive at all")

    assert response.status_code == 415
    assert leftover_files(tmp_archive_dir) == []


def test_an_empty_upload_is_refused(client, tmp_archive_dir):
    response = post_archive(client, b"")

    assert response.status_code == 415
    assert leftover_files(tmp_archive_dir) == []


# --------------------------------------------------------------------------- #
# admission
# --------------------------------------------------------------------------- #
def test_backlog_watermark_refuses_an_archive(client, monkeypatch):
    gate = upload_admission.UploadAdmission(limit=4, high_watermark=1, depth_provider=lambda: 9)
    monkeypatch.setattr(ingest_api.upload_admission, "get_admission", lambda: gate)

    response = post_archive(client, build_zip([("a.pdf", PDF_A)]))

    assert response.status_code == 429
    assert response.headers["Retry-After"]


def test_the_slot_is_released_after_a_failed_archive(client, monkeypatch):
    gate = upload_admission.UploadAdmission(limit=1, depth_provider=lambda: 0)
    monkeypatch.setattr(ingest_api.upload_admission, "get_admission", lambda: gate)

    post_archive(client, SEVEN_ZIP_MAGIC + b"\x00" * 64)

    assert gate.in_flight == 0
