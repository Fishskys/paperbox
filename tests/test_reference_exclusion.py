"""The bibliography exclusion: default-on, flag-off, and clause shape.

Pure body checks (no cluster): the exclusion must appear in the keyword leg's
``bool.must_not``, inside the kNN clause filter, and in the native hybrid
``filter.bool.must_not``; ``include_references: true`` must remove it
everywhere. The Abstract section is asserted as NOT excluded.
"""

from app.search import hybrid
from app.search.native import build_native_body
from app.schemas.search import SearchFilters


def _excluded_sections(body: dict) -> list[str] | None:
    """Collect the ``section`` terms values from all ``must_not`` clauses."""
    values: list[str] = []
    found = False

    def walk(node) -> None:
        nonlocal found
        if isinstance(node, dict):
            if "must_not" in node:
                for clause in node["must_not"]:
                    terms = clause.get("terms", {})
                    if "section" in terms:
                        found = True
                        values.extend(terms["section"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return values if found else None


def test_reference_exclusion_is_on_by_default() -> None:
    body = hybrid.build_keyword_query("transformer")
    excluded = _excluded_sections(body)
    assert excluded is not None
    assert "References" in excluded
    assert "REFERENCES" in excluded
    assert "Bibliography" in excluded
    assert "Abstract" not in excluded


def test_reference_exclusion_also_in_the_knn_leg() -> None:
    body = hybrid.build_semantic_query([0.0] * 4)
    excluded = _excluded_sections(body)
    assert excluded is not None
    assert "References" in excluded


def test_reference_exclusion_also_in_the_native_hybrid() -> None:
    body = build_native_body("transformer", [0.0] * 4, size=10)
    excluded = _excluded_sections(body)
    assert excluded is not None
    assert "References" in excluded


def test_include_references_flag_turns_the_exclusion_off() -> None:
    filters = SearchFilters(include_references=True).to_query_filters()
    assert hybrid.build_reference_exclusion(filters) == []
    assert _excluded_sections(hybrid.build_keyword_query("lstm", filters)) is None
    assert (
        _excluded_sections(hybrid.build_semantic_query([0.0] * 4, filters)) is None
    )
    assert (
        _excluded_sections(build_native_body("lstm", [0.0] * 4, filters, size=10))
        is None
    )


def test_no_filters_still_excludes() -> None:
    """The default path (``filters=None``) must exclude too."""
    assert _excluded_sections(hybrid.build_keyword_query("cnn")) is not None
    assert _excluded_sections(hybrid.build_semantic_query([0.0] * 4)) is not None
    assert _excluded_sections(build_native_body("cnn", [0.0] * 4, size=10)) is not None


def test_case_variants_cover_the_corpus() -> None:
    """The corpus carries ``References`` (433) and ``REFERENCES`` (90)."""
    excluded = _excluded_sections(hybrid.build_keyword_query("rnn"))
    assert set(excluded or []) >= {"References", "REFERENCES", "references"}
