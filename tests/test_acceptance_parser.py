"""Unit tests for the T8 acceptance helpers (no docker, no MinIO, no network).

Only the pure parts of ``scripts/acceptance_parser.py`` are exercised: the
markdown statistics, the diff summary, the memory-string parser and the store
totals delta. Running the script itself against real papers is deliberate human
work and lives outside pytest.
"""

from __future__ import annotations

import pytest

from scripts.acceptance_parser import (
    describe_markdown,
    formula_blocks,
    parse_mem_usage,
    summarize_diff,
    table_blocks,
    totals_delta,
)

PAGE_BREAK = "<!-- page-break -->"


def test_page_markers_must_equal_pages_minus_one() -> None:
    markdown = "text\n\n<!-- page-break -->\n\nmore\n\n<!-- page-break -->\n\ntail"
    stats = describe_markdown(markdown, pages=3, page_break=PAGE_BREAK)
    assert stats["page_markers"] == 2
    assert stats["page_markers_expected"] == 2
    assert stats["page_markers_ok"] is True


def test_page_marker_mismatch_is_flagged() -> None:
    stats = describe_markdown("one page only", pages=4, page_break=PAGE_BREAK)
    assert stats["page_markers"] == 0
    assert stats["page_markers_expected"] == 3
    assert stats["page_markers_ok"] is False


def test_single_page_document_expects_no_marker() -> None:
    stats = describe_markdown("# Title\n\nbody", pages=1, page_break=PAGE_BREAK)
    assert stats["page_markers_expected"] == 0
    assert stats["page_markers_ok"] is True


def test_headings_counted_with_max_level() -> None:
    markdown = "# Paper\n\n## I. Intro\n\n### A. Detail\n\n#### Too deep"
    stats = describe_markdown(markdown, pages=1, page_break=PAGE_BREAK)
    assert stats["headings"] == 4
    assert stats["max_heading_level"] == 4


def test_table_blocks_count_blocks_not_rows() -> None:
    markdown = "before\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nafter\n\n| x | y |\n| --- | --- |\n"
    assert table_blocks(markdown) == 2


def test_table_blocks_zero_when_no_table() -> None:
    assert table_blocks("# Title\n\nplain text\n") == 0


def test_table_fallback_marker_is_counted_separately() -> None:
    from app.parsing.markdown import TABLE_FALLBACK_MARKER

    markdown = f"a\n\n{TABLE_FALLBACK_MARKER}\n\nb\n\n{TABLE_FALLBACK_MARKER}\n"
    stats = describe_markdown(markdown, pages=1, page_break=PAGE_BREAK)
    assert stats["tables"] == 0
    assert stats["table_marks"] == 2


def test_formula_blocks_count_dollar_pairs() -> None:
    markdown = "text\n\n$$E = mc^2$$\n\nmore\n\n$$a + b$$\n"
    assert formula_blocks(markdown) == 2


def test_odd_dollar_count_rounds_down() -> None:
    assert formula_blocks("$$\nunclosed") == 0


def test_summarize_diff_counts_added_and_removed_lines() -> None:
    text, summary = summarize_diff("a\nb\nc", "a\nB\nc", name_left="x", name_right="y")
    # 3 header/hunk lines + 2 context + 1 removed + 1 added
    assert summary == {"diff_lines": 7, "added": 1, "removed": 1}
    assert "-b" in text and "+B" in text


def test_summarize_diff_of_identical_text_is_empty() -> None:
    text, summary = summarize_diff("same", "same", name_left="x", name_right="y")
    assert text == ""
    assert summary["added"] == 0 and summary["removed"] == 0


def test_parse_mem_usage_reads_docker_stats_output() -> None:
    assert parse_mem_usage("2.06GiB / 8GiB") == pytest.approx(2.06 * 1024**3)
    assert parse_mem_usage("512MiB / 1GiB") == pytest.approx(512 * 1024**2)
    assert parse_mem_usage("1.5GB / 2GB") == pytest.approx(1.5 * 1000**3)


def test_parse_mem_usage_rejects_unparsable_input() -> None:
    assert parse_mem_usage("--") is None
    assert parse_mem_usage("nonsense") is None
    assert parse_mem_usage("abcGiB / 8GiB") is None


def test_totals_delta_reports_only_changed_keys() -> None:
    before = {"papers_live": 3, "objects_minio": 10, "chunks_pg": 40}
    assert totals_delta(before, dict(before)) == {}
    delta = totals_delta(before, {**before, "objects_minio": 12})
    assert delta == {"objects_minio": (10, 12)}


def test_totals_delta_is_empty_without_both_sides() -> None:
    assert totals_delta(None, {"a": 1}) == {}
    assert totals_delta({"a": 1}, None) == {}

