"""Unit tests for the pure parts of ``scripts/acceptance_mcp.py``.

The script itself is run against a live server as part of T-A13; what is tested here is
everything that can be decided without one -- most importantly the cleanup rule, since
getting that wrong means deleting a real paper.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acceptance_mcp.py"


def _load():
    spec = importlib.util.spec_from_file_location("acceptance_mcp", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["acceptance_mcp"] = module
    spec.loader.exec_module(module)
    return module


acceptance = _load()


# -- the probe PDF ----------------------------------------------------------------


def test_synthetic_pdf_is_a_readable_pdf() -> None:
    pypdf = pytest.importorskip("pypdf")
    import io

    marker = "paperbox acceptance probe 3f2a"
    reader = pypdf.PdfReader(io.BytesIO(acceptance.synthetic_pdf(marker)))
    assert len(reader.pages) == 1
    assert marker in reader.pages[0].extract_text()


def test_synthetic_pdf_differs_per_marker() -> None:
    assert acceptance.synthetic_pdf("one") != acceptance.synthetic_pdf("two")


def test_synthetic_pdf_is_deterministic() -> None:
    assert acceptance.synthetic_pdf("same") == acceptance.synthetic_pdf("same")


def test_synthetic_pdf_escapes_parentheses() -> None:
    """A marker with PDF syntax characters must not break the content stream."""
    pypdf = pytest.importorskip("pypdf")
    import io

    reader = pypdf.PdfReader(io.BytesIO(acceptance.synthetic_pdf("a (b) \\ c")))
    assert "a (b) \\ c" in reader.pages[0].extract_text()


# -- the cleanup rule -------------------------------------------------------------


def test_cleanup_refuses_a_pre_existing_paper() -> None:
    allowed, why = acceptance.decide_cleanup("p1", {"p1", "p2"})
    assert allowed is False
    assert "existed before" in why


def test_cleanup_allows_a_paper_this_run_created() -> None:
    allowed, why = acceptance.decide_cleanup("p3", {"p1", "p2"})
    assert allowed is True
    assert why == "created by this run"


def test_cleanup_refuses_when_nothing_was_created() -> None:
    allowed, _ = acceptance.decide_cleanup(None, {"p1"})
    assert allowed is False


def test_missing_and_new_ids() -> None:
    before = {"a", "b", "c"}
    after = {"b", "c", "d"}
    assert acceptance.missing_from(before, after) == {"a"}
    assert acceptance.new_ids(before, after) == {"d"}


# -- error parsing ----------------------------------------------------------------


def test_error_code_reads_the_sdk_prefixed_payload() -> None:
    text = (
        "Error executing tool paper_get: "
        '{"error": {"code": "NOT_FOUND", "message": "no such paper", "retryable": false}}'
    )
    assert acceptance.error_code(text) == "NOT_FOUND"


def test_error_code_handles_a_flat_payload() -> None:
    assert acceptance.error_code('noise {"code": "INVALID_ARGUMENT"} more noise') == "INVALID_ARGUMENT"


def test_error_code_is_blank_when_there_is_no_json() -> None:
    assert acceptance.error_code("Error executing tool paper_get: boom") == ""


# -- where the probe PDF may live -------------------------------------------------


def test_ingest_roots_split_like_the_server() -> None:
    assert acceptance.parse_ingest_roots(r"C:\a, D:\b\ ,") == [r"C:\a", r"D:\b"]


def test_probe_dir_prefers_the_explicit_argument(tmp_path) -> None:
    directory, why = acceptance.default_probe_dir(str(tmp_path), r"C:\allowed", tmp_path / "nope")
    assert directory == str(tmp_path)
    assert "--probe-dir" in why


def test_probe_dir_falls_back_to_the_environment(tmp_path) -> None:
    directory, _ = acceptance.default_probe_dir("", r"C:\first, C:\second", tmp_path / "nope")
    assert directory == r"C:\first"


def test_probe_dir_reads_the_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\nINGEST_LOCAL_ROOTS=D:\\papers\n", encoding="utf-8")
    directory, why = acceptance.default_probe_dir("", "", env_file)
    assert directory == "D:\\papers"
    assert "INGEST_LOCAL_ROOTS" in why


def test_probe_dir_is_empty_without_any_root(tmp_path) -> None:
    directory, why = acceptance.default_probe_dir("", "", tmp_path / "missing.env")
    assert directory == ""
    assert "--probe-dir" in why


# -- report -----------------------------------------------------------------------


def test_exit_code_only_succeeds_when_nothing_failed() -> None:
    report = acceptance.Report()
    report.add("a", True)
    assert acceptance.exit_code(report) == 0
    report.add("b", False, "detail")
    assert acceptance.exit_code(report) == 1
    assert [check.label for check in report.failed] == ["b"]
