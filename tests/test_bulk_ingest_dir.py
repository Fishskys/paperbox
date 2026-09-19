"""``scripts/bulk_ingest_dir.py``: the client side of a folder import (2026-09-19).

The script's interesting logic is pure and lives outside the HTTP calls, so it
can be tested without a server or a running app: which files are candidates,
what the manifest looks like, how long to wait after a ``429``, what
``--resume`` should skip, and how the report counts add up.

The end-to-end behaviour (``--via-http`` uploads, server-side ``/ingest/dir``
import, job polling) is exercised against a live instance in the acceptance run;
here it is the arithmetic and the filtering that are pinned down.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bulk_ingest_dir as script  # noqa: E402


# --------------------------------------------------------------------------- #
# filtering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,expected",
    [
        ("paper.pdf", True),
        ("PAPER.PDF", True),
        ("nested/deep.pdf", True),
        ("notes.txt", False),
        ("archive.zip", False),
        (".hidden.pdf", False),
        ("draft.pdf.tmp", False),
        ("download.pdf.part", False),
        ("swap.pdf~", False),
        ("", False),
    ],
)
def test_is_candidate(name, expected):
    assert script.is_candidate(name) is expected


@pytest.mark.parametrize(
    "relative,pattern,expected",
    [
        ("a.pdf", "**/*.pdf", True),
        ("nested/a.pdf", "**/*.pdf", True),
        ("a.txt", "**/*.pdf", False),
        ("nested/a.pdf", "*.pdf", False),
        ("a.pdf", "*.pdf", True),
        ("nested/a.pdf", "nested/*.pdf", True),
        ("deep/nested/a.pdf", "nested/*.pdf", False),
    ],
)
def test_matches_glob(relative, pattern, expected):
    assert script.matches_glob(relative, pattern) is expected


def test_collect_files_walks_and_prescreens(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7 a")
    (tmp_path / "nested" / "b.PDF").write_bytes(b"%PDF-1.7 b")
    (tmp_path / "notes.txt").write_bytes(b"nope")
    (tmp_path / "draft.pdf.tmp").write_bytes(b"%PDF-1.7 tmp")
    (tmp_path / ".hidden.pdf").write_bytes(b"%PDF-1.7 hidden")

    found = script.collect_files(tmp_path)

    assert [path.name for path in found] == ["a.pdf", "b.PDF"]


def test_collect_files_honours_recursive_and_limit(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7 a")
    (tmp_path / "nested" / "b.pdf").write_bytes(b"%PDF-1.7 b")
    (tmp_path / "nested" / "c.pdf").write_bytes(b"%PDF-1.7 c")

    assert [p.name for p in script.collect_files(tmp_path, recursive=False)] == ["a.pdf"]
    assert len(script.collect_files(tmp_path, limit=2)) == 2


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def test_build_manifest_splits_candidates_from_skips(tmp_path):
    (tmp_path / "ok.pdf").write_bytes(b"%PDF-1.7 ok")
    (tmp_path / "empty.pdf").write_bytes(b"")
    (tmp_path / "big.pdf").write_bytes(b"%PDF-1.7" + b"x" * 500)

    candidates, skipped = script.build_manifest(
        [tmp_path / "ok.pdf", tmp_path / "empty.pdf", tmp_path / "big.pdf"],
        tmp_path,
        max_bytes=100,
    )

    assert [entry["relative"] for entry in candidates] == ["ok.pdf"]
    reasons = {entry["relative"]: entry["reason"] for entry in skipped}
    assert reasons == {"empty.pdf": "empty file", "big.pdf": "larger than the 100 byte limit"}
    assert all(entry["status"] == "skipped" for entry in skipped)


def test_build_manifest_records_relative_and_absolute_paths(tmp_path):
    nested = tmp_path / "sub"
    nested.mkdir()
    path = nested / "x.pdf"
    path.write_bytes(b"%PDF-1.7")

    candidates, _ = script.build_manifest([path], tmp_path)

    assert candidates[0]["relative"] == "sub/x.pdf"
    assert Path(candidates[0]["path"]) == path
    assert candidates[0]["size_bytes"] == 8


# --------------------------------------------------------------------------- #
# backoff
# --------------------------------------------------------------------------- #
def test_retry_after_wins_over_backoff():
    assert script.retry_delay(1, retry_after=2.0) == 2.0
    assert script.retry_delay(9, retry_after=3.5) == 3.5


def test_retry_after_is_capped():
    assert script.retry_delay(1, retry_after=10_000) == script.BACKOFF_CAP


def test_backoff_grows_and_is_capped():
    delays = [script.retry_delay(attempt, jitter=0.0) for attempt in range(1, 10)]

    assert delays[0] == script.DEFAULT_RETRY_AFTER
    assert delays[1] == 2 * script.DEFAULT_RETRY_AFTER
    assert delays == sorted(delays)
    assert max(delays) <= script.BACKOFF_CAP


def test_backoff_adds_a_little_jitter():
    values = {script.retry_delay(3) for _ in range(50)}

    assert len(values) > 1


def test_parse_retry_after_reads_seconds_and_ignores_dates():
    assert script.parse_retry_after("2") == 2.0
    assert script.parse_retry_after(" 1.5 ") == 1.5
    assert script.parse_retry_after(None) is None
    assert script.parse_retry_after("") is None
    assert script.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert script.parse_retry_after("-3") is None


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #
def test_completed_paths_collects_finished_entries():
    report = {
        "items": [
            {"relative": "a.pdf", "status": "accepted", "stage": "COMPLETED"},
            {"relative": "b.pdf", "status": "accepted", "stage": "FAILED"},
            {"relative": "c.pdf", "status": "duplicate", "stage": None},
            {"relative": "d.pdf", "status": "skipped", "reason": "empty file"},
            {"path": "e.pdf", "status": "accepted", "stage": "COMPLETED"},
        ]
    }

    assert script.completed_paths(report) == {"a.pdf", "c.pdf", "e.pdf"}


def test_completed_paths_handles_a_missing_report():
    assert script.completed_paths(None) == set()
    assert script.completed_paths({}) == set()


def test_resume_round_trips_through_the_report_file(tmp_path):
    out = tmp_path / "report.json"
    out.write_text(
        json.dumps(
            {
                "items": [
                    {"relative": "done.pdf", "status": "accepted", "stage": "COMPLETED"},
                    {"relative": "todo.pdf", "status": "accepted", "stage": "FAILED"},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    done = script.completed_paths(report)
    manifest = [{"relative": "done.pdf"}, {"relative": "todo.pdf"}]

    assert [entry["relative"] for entry in manifest if entry["relative"] not in done] == [
        "todo.pdf"
    ]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def test_summarize_counts_every_outcome():
    items = [
        {"status": "accepted", "stage": "COMPLETED"},
        {"status": "accepted", "stage": "FAILED"},
        {"status": "duplicate"},
        {"status": "rejected", "error_code": "UNSUPPORTED_TYPE"},
    ]

    counts = script.summarize(items, skipped=2)

    assert counts == {
        "total": 6,
        "accepted": 2,
        "duplicate": 1,
        "rejected": 1,
        "completed": 1,
        "failed": 1,
        "skipped": 2,
    }


def test_format_console_groups_failures_by_code():
    report = {
        "mode": "server-side",
        "root": "/papers",
        "counts": {
            "total": 3,
            "accepted": 3,
            "duplicate": 0,
            "rejected": 0,
            "completed": 1,
            "failed": 2,
            "skipped": 0,
        },
        "items": [
            {"relative": "a.pdf", "stage": "COMPLETED"},
            {"relative": "b.pdf", "stage": "FAILED", "error_code": "NO_TEXT_LAYER"},
            {"relative": "c.pdf", "stage": "FAILED", "error_code": "NO_TEXT_LAYER"},
        ],
    }

    text = script.format_console(report)

    assert "NO_TEXT_LAYER" in text
    assert "failed 2" in text


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_dry_run_sends_nothing(tmp_path, capsys):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7 a")
    (tmp_path / "notes.txt").write_bytes(b"nope")

    code = script.main(["--root", str(tmp_path), "--dry-run", "--base-url", "http://127.0.0.1:1"])

    assert code == 0
    out = capsys.readouterr().out
    assert "would import a.pdf" in out
    assert "1 file(s)" in out


def test_a_missing_root_is_a_usage_error(tmp_path, capsys):
    code = script.main(["--root", str(tmp_path / "nope"), "--dry-run"])

    assert code == 2
    assert "not a directory" in capsys.readouterr().err


def test_a_non_positive_limit_is_a_usage_error(tmp_path, capsys):
    code = script.main(["--root", str(tmp_path), "--limit", "0", "--dry-run"])

    assert code == 2
    assert "positive" in capsys.readouterr().err
