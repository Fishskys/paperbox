"""JSONL imports share JSON's metadata, preview and deduplication behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.models import Paper, PaperSource
from app.services import metadata_import as importer
from scripts import import_metadata
from tests.test_metadata_api import client  # noqa: F401 - fixture

EXAMPLES = Path(__file__).parent / "metadata"
JSON_FILE = EXAMPLES / "ieee_metadata_example.json"
JSONL_FILE = EXAMPLES / "ieee_metadata_example.jsonl"
TITLE = (
    "A Hybrid-Domain Two-Step Time-to-Digital Converter Using a Switch-Based "
    "Time-to-Voltage Converter and SAR ADC"
)


@pytest.mark.parametrize("path", [JSON_FILE, JSONL_FILE], ids=["json", "jsonl"])
def test_ieee_file_preview_does_not_write(path, db_session):
    report = importer.import_file(db_session, path)

    assert report.format == "ieee_raw"
    assert report.dry_run is True
    assert (report.detected, report.created_shell, report.failed) == (10, 10, 0)
    assert db_session.query(Paper).count() == 0
    assert db_session.query(PaperSource).count() == 0


def test_importing_json_then_jsonl_does_not_duplicate_papers(db_session):
    importer.import_file(db_session, JSON_FILE, apply=True)
    report = importer.import_file(db_session, JSONL_FILE, apply=True)

    assert report.unchanged == 10
    assert db_session.query(Paper).count() == 10
    assert db_session.query(PaperSource).count() == 10
    paper = db_session.query(Paper).filter_by(title=TITLE).one()
    assert paper.year == 2015
    assert paper.pages == "631-635"
    assert paper.venue.name == "IEEE Transactions on Circuits and Systems II: Express Briefs"
    assert [link.author.name for link in sorted(paper.paper_authors, key=lambda x: x.author_order)] == [
        "Jungho Kim", "Young-Hwa Kim", "KwangSeok Kim", "Wonsik Yu", "SeongHwan Cho",
    ]


@pytest.mark.parametrize("transport", [
    "upload", "application/x-ndjson", "application/ndjson", "application/jsonl", "application/json",
])
def test_api_imports_ten_ieee_jsonl_records(transport, client, session_factory):
    data = JSONL_FILE.read_bytes()
    if transport == "upload":
        response = client.post(
            "/api/metadata/import?apply=true",
            files={"file": ("ieee.jsonl", data, "application/octet-stream")},
        )
    else:
        response = client.post(
            "/api/metadata/import?apply=true", content=data,
            headers={"Content-Type": transport + "; charset=utf-8"},
        )

    assert response.status_code == 200
    report = response.json()
    assert report["format"] == "ieee_raw"
    assert (report["detected"], report["created_shell"], report["failed"]) == (10, 10, 0)
    with session_factory() as session:
        assert session.query(Paper).count() == 10
        source = session.query(PaperSource).filter(PaperSource.raw["title"].as_string() == TITLE).one()
        assert source.raw["article_number"] == "7065247"
        assert source.raw["authors"]["authors"][0]["full_name"] == "Jungho Kim"


@pytest.mark.parametrize("record,expected_format", [
    ({"title": "Single CSL record", "DOI": "10.1234/jsonl", "type": "article-journal"}, "csl_json"),
    ({"title": "Single generic record", "doi": "10.1234/jsonl", "year": 2025}, "generic"),
])
@pytest.mark.parametrize("transport", ["body", "upload"])
def test_single_line_jsonl_keeps_the_record_format(record, expected_format, transport, client, session_factory):
    if transport == "body":
        data = ("\ufeff\r\n" + json.dumps(record) + "\r\n\r\n").encode("utf-8")
        response = client.post(
            "/api/metadata/import?apply=true", content=data,
            headers={"Content-Type": "application/x-ndjson"},
        )
    else:
        data = ("\ufeff" + json.dumps(record)).encode("utf-8")
        response = client.post(
            "/api/metadata/import?apply=true",
            files={"file": ("single.jsonl", data, "application/octet-stream")},
        )

    assert response.status_code == 200
    assert response.json()["format"] == expected_format
    assert response.json()["detected"] == 1
    with session_factory() as session:
        assert session.query(Paper).one().title == record["title"]
        assert session.query(PaperSource).one().raw == record


@pytest.mark.parametrize("invalid_line", ["{broken}", "null", '[]', '"not an object"'])
def test_bad_jsonl_line_reports_its_number_without_partial_writes(invalid_line, client, session_factory):
    response = client.post(
        "/api/metadata/import?apply=true",
        content='{"title":"Valid first record"}\n\n' + invalid_line,
        headers={"Content-Type": "application/x-ndjson"},
    )

    assert response.status_code == 422
    assert "line 3" in response.json()["detail"]
    with session_factory() as session:
        assert session.query(Paper).count() == 0
        assert session.query(PaperSource).count() == 0


def test_cli_imports_jsonl_and_honors_the_limit(monkeypatch, session_factory, tmp_path, capsys):
    report_file = tmp_path / "report.json"
    monkeypatch.setattr(import_metadata, "SessionLocal", session_factory)
    monkeypatch.setattr(import_metadata.sys, "argv", [
        "import_metadata.py", str(JSONL_FILE), "--apply", "--limit", "2",
        "--report", str(report_file),
    ])

    assert import_metadata.main() == 0
    report = json.loads(report_file.read_text())
    assert (report["detected"], report["total"], report["skipped"]) == (10, 2, 8)
    assert report["dry_run"] is False
    assert "applied" in capsys.readouterr().out
    with session_factory() as session:
        assert session.query(Paper).count() == 2
        assert session.query(PaperSource).count() == 2
