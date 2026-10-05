"""``POST /api/papers/ingest/dir``: the server imports a directory itself (2026-09-19).

When the PDFs already live on the machine that runs paperbox (or on a mounted
volume), a 1000-file import should transfer zero bytes: the endpoint walks the
directory, hashes each candidate, drops the ones already in the library and
queues the rest as ``local_path`` jobs.

Reading the server's filesystem is a new attack surface, so the endpoint is
off by default (``INGEST_LOCAL_ROOTS`` empty -> ``404``) and every path must
resolve inside one of the whitelisted roots (``403`` otherwise). Symlinks are
never followed -- not while walking, and not for the file itself -- because a
symlink inside a whitelisted root is the obvious way to escape it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import ingestion as ingest_api
from app.core.config import settings
from app.core.security import require_api_key, require_write
from app.db.models import IngestionJob
from app.db.session import get_db
from app.main import app
from app.services import object_storage, upload_admission
from tests.test_ingest_files import QueueRecorder  # noqa: F401 - fixture helper
from tests.test_local_source import (  # noqa: F401 - fixtures
    factory,
    make_paper_with_sha256,
)

PDF_HEAD = b"%PDF-1.7\n"


def pdf_bytes(tag: str, size: int = 512) -> bytes:
    """A PDF-ish payload of exactly ``size`` bytes, unique per ``tag``."""
    body = (tag.encode() * size)[: max(1, size - len(PDF_HEAD))]
    return PDF_HEAD + body


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def tree(tmp_path) -> Path:
    """A small directory tree with the cases the scan must handle."""
    root = tmp_path / "papers"
    (root / "nested").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a.pdf").write_bytes(pdf_bytes("a"))
    (root / "b.pdf").write_bytes(pdf_bytes("b"))
    (root / "notes.txt").write_bytes(b"not a pdf")
    (root / "nested" / "c.pdf").write_bytes(pdf_bytes("c"))
    (root / "nested" / "d.PDF").write_bytes(pdf_bytes("d"))
    (root / ".hidden" / "e.pdf").write_bytes(pdf_bytes("e"))
    (root / ".secret.pdf").write_bytes(pdf_bytes("secret"))
    (root / "draft.pdf.tmp").write_bytes(pdf_bytes("tmp"))
    return root


@pytest.fixture()
def whitelist(monkeypatch, tree):
    """Point ``INGEST_LOCAL_ROOTS`` at the fixture tree."""
    root = os.path.realpath(str(tree))
    monkeypatch.setattr(settings, "ingest_local_roots", root)
    return Path(root)


@pytest.fixture()
def queued(monkeypatch):
    recorder = QueueRecorder()
    monkeypatch.setattr(ingest_api.job_queue, "submit", recorder)
    return recorder


@pytest.fixture()
def no_storage(monkeypatch):
    """Any object-storage use in these tests is a bug (nothing is uploaded)."""
    def boom(*args, **kwargs):
        raise AssertionError("directory import must not touch object storage")

    monkeypatch.setattr(ingest_api.object_storage, "upload_bytes", boom)
    monkeypatch.setattr(ingest_api.object_storage, "upload_stream_hashed", boom)


@pytest.fixture()
def client(factory, whitelist, queued, no_storage, monkeypatch):  # noqa: F811
    gate = upload_admission.UploadAdmission(limit=8, depth_provider=lambda: 0)
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


def make_dir_link(link: Path, target: Path) -> None:
    """Create a directory link, or skip the test if the OS will not allow it.

    ``os.symlink`` needs Developer Mode (or admin) on Windows, while ``mklink /J``
    creates a junction unprivileged -- and a junction is the reparse point
    ``os.walk(followlinks=False)`` *does* descend into, so it is the case that
    actually matters on this platform.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0 or not link.is_dir():
        pytest.skip(f"cannot create a directory link here: {result.stdout}{result.stderr}")


def post_dir(client, **body):
    payload = {"root": body.pop("root")}
    payload.update(body)
    return client.post("/api/papers/ingest/dir", json=payload)


def job_payloads(factory, job_ids):  # noqa: F811
    session = factory()
    try:
        return [dict(session.get(IngestionJob, job_id).payload) for job_id in job_ids]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_directory_import_queues_local_path_jobs(client, tree, queued, factory):  # noqa: F811
    response = post_dir(client, root=str(tree))

    assert response.status_code == 202
    body = response.json()
    assert body["dry_run"] is False
    assert body["accepted"] == 4  # a, b, nested/c, nested/d.PDF
    assert body["duplicate"] == 0
    assert body["rejected"] == 0
    assert body["matched"] == 4

    names = sorted(entry["filename"] for entry in body["jobs"])
    assert names == ["a.pdf", "b.pdf", "c.pdf", "d.PDF"]

    payloads = job_payloads(factory, [entry["job_id"] for entry in body["jobs"]])
    assert all(item["source_type"] == "local_path" for item in payloads)
    assert all(Path(item["local_path"]).is_file() for item in payloads)
    assert all(item.get("cleanup_after") is None for item in payloads)
    assert len(queued.submitted) == 4
    # A folder dump is batch work.
    assert [priority for _, _, priority in queued.submitted] == [1, 1, 1, 1]


def test_scan_skips_hidden_temporary_and_non_pdf_files(client, tree):
    body = post_dir(client, root=str(tree)).json()

    listed = {entry["filename"] for entry in body["jobs"]}
    assert ".secret.pdf" not in listed
    assert "draft.pdf.tmp" not in listed
    assert "notes.txt" not in listed
    assert not any(name.startswith(".hidden") for name in listed)


def test_recursive_false_only_scans_the_top_level(client, tree):
    body = post_dir(client, root=str(tree), recursive=False, glob="*.pdf").json()

    assert sorted(entry["filename"] for entry in body["jobs"]) == ["a.pdf", "b.pdf"]


def test_glob_selects_a_subset(client, tree):
    body = post_dir(client, root=str(tree), glob="nested/c*.pdf").json()

    assert [entry["relative"] for entry in body["jobs"]] == ["nested/c.pdf"]


def test_glob_is_case_insensitive_for_the_extension(client, tree):
    body = post_dir(client, root=str(tree), glob="nested/*.pdf").json()

    assert sorted(entry["relative"] for entry in body["jobs"]) == [
        "nested/c.pdf",
        "nested/d.PDF",
    ]


def test_limit_caps_the_import_and_reports_the_rest_as_skipped(client, tree):
    body = post_dir(client, root=str(tree), limit=2).json()

    assert body["matched"] == 4
    assert body["accepted"] == 2
    assert body["skipped"] == 2


def test_a_single_matched_file_is_interactive(client, tree, queued):
    post_dir(client, root=str(tree), glob="a.pdf")

    assert [priority for _, _, priority in queued.submitted] == [0]


# --------------------------------------------------------------------------- #
# dedupe and rejection
# --------------------------------------------------------------------------- #
def test_content_already_in_the_library_is_reported_as_duplicate(
    client, tree, factory, queued  # noqa: F811
):
    digest = hashlib.sha256(pdf_bytes("a")).hexdigest()
    existing_id = make_paper_with_sha256(factory, digest)

    body = post_dir(client, root=str(tree), glob="a.pdf").json()

    assert (body["accepted"], body["duplicate"]) == (0, 1)
    item = body["jobs"][0]
    assert item["status"] == "duplicate"
    assert item["paper_id"] == existing_id
    assert item["job_id"] is None  # no job is created for a duplicate
    assert queued.submitted == []


def test_oversized_files_are_rejected_per_file(client, tree, monkeypatch):
    monkeypatch.setattr(ingest_api.ingest, "max_file_bytes", lambda: 200)

    body = post_dir(client, root=str(tree)).json()

    assert body["rejected"] == 4
    assert {entry["error_code"] for entry in body["jobs"]} == {"OVERSIZED"}
    assert body["accepted"] == 0


def test_an_unreadable_file_does_not_stop_the_scan(client, tree, monkeypatch):
    real_hash = ingest_api.paper_service.compute_sha256_file

    def flaky(path, **kwargs):
        if Path(path).name == "a.pdf":
            raise OSError("permission denied")
        return real_hash(path, **kwargs)

    monkeypatch.setattr(ingest_api.paper_service, "compute_sha256_file", flaky)

    body = post_dir(client, root=str(tree)).json()

    assert body["accepted"] == 3
    assert body["rejected"] == 1
    assert body["jobs"][0]["error_code"] == "INTERNAL"


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #
def test_dry_run_reports_the_plan_without_creating_anything(
    client, tree, queued, factory  # noqa: F811
):
    body = post_dir(client, root=str(tree), dry_run=True).json()

    assert body["dry_run"] is True
    assert body["accepted"] == 4
    assert all(entry["job_id"] is None for entry in body["jobs"])
    assert queued.submitted == []

    session = factory()
    try:
        assert session.query(IngestionJob).count() == 0
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# whitelist
# --------------------------------------------------------------------------- #
def test_disabled_endpoint_answers_404(client, monkeypatch):
    monkeypatch.setattr(settings, "ingest_local_roots", "")

    response = post_dir(client, root=str(Path(os.path.realpath(os.sep))))

    assert response.status_code == 404
    assert "disabled" in response.json()["detail"].lower()


def test_root_outside_the_whitelist_answers_403(client, tmp_path, tree):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.pdf").write_bytes(pdf_bytes("x"))

    response = post_dir(client, root=str(outside))

    assert response.status_code == 403
    assert "whitelist" in response.json()["detail"].lower()


def test_parent_traversal_is_rejected(client, tree):
    escape = str(tree / ".." / ".." / "..")

    response = post_dir(client, root=escape)

    assert response.status_code == 403


def test_a_linked_directory_cannot_escape_the_whitelist(client, tmp_path, tree):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.pdf").write_bytes(pdf_bytes("evil"))
    link = tree / "link"
    make_dir_link(link, outside)

    response = post_dir(client, root=str(link))

    assert response.status_code == 403


def test_a_linked_directory_inside_the_root_is_not_walked(client, tmp_path, tree):
    """The walk must not descend into a junction/symlink that leaves the root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.pdf").write_bytes(pdf_bytes("evil"))
    make_dir_link(tree / "escape", outside)

    body = post_dir(client, root=str(tree)).json()

    assert body["accepted"] == 4
    assert "evil.pdf" not in {entry["filename"] for entry in body["jobs"]}


def test_a_symlinked_pdf_inside_the_root_is_skipped(client, tmp_path, tree):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.pdf").write_bytes(pdf_bytes("evil"))
    link = tree / "innocent.pdf"
    try:
        link.symlink_to(outside / "evil.pdf")
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        pytest.skip("file symlinks are not available on this machine")

    body = post_dir(client, root=str(tree)).json()

    assert "innocent.pdf" not in {entry["filename"] for entry in body["jobs"]}


def test_a_missing_directory_answers_404(client, tree):
    response = post_dir(client, root=str(tree / "nope"))

    assert response.status_code == 404


def test_several_whitelisted_roots_are_accepted(client, tmp_path, tree, monkeypatch):
    second = tmp_path / "second"
    second.mkdir()
    (second / "s.pdf").write_bytes(pdf_bytes("s"))
    monkeypatch.setattr(
        settings,
        "ingest_local_roots",
        f"{os.path.realpath(str(tree))};{os.path.realpath(str(second))}",
    )

    body = post_dir(client, root=str(second)).json()

    assert body["accepted"] == 1
