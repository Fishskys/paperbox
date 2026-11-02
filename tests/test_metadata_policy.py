"""Conservative equivalence must not erase real disagreements or history."""

import pytest

from app.services import metadata_policy as policy


@pytest.mark.parametrize("field,left,right", [
    ("title", "A Flash–TDC & ADC", "a flash-TDC &amp; ADC"),
    ("title", "A 0.48mm² TDC", "A 0.48mm<sup>2</sup> TDC"),
    ("title", "An italic N", 'An <italic xmlns:x="urn:x">italic</italic> N'),
    ("authors", ["José Chen"], ["Jose\u0301 Chen"]),
    ("authors", ["E.A. Wulf", "J.D. Kurfess"], ["E. A. Wulf", "J. D. Kurfess"]),
    ("authors", ["L.H.C.Braga"], ["L. H. C. Braga"]),
    ("identifier:doi", "https://doi.org/10.1109/TEST.2020.1", "10.1109/test.2020.1"),
    ("tag:author_terms", ["TDC", "ADC"], ["adc", "tdc", "TDC"]),
])
def test_presentation_differences_compare_equal(field, left, right):
    assert policy.equivalent_values(field, left, right)


@pytest.mark.parametrize("field,left,right", [
    ("authors", ["Alice", "Bob"], ["Bob", "Alice"]),
    ("authors", ["Alice", "Bob"], ["Alice"]),
    ("authors", ["Händel"], ["Handel"]),
    ("authors", ["A. Smith"], ["Alice Smith"]),
    ("authors", ["Ann-Marie Smith"], ["Ann Marie Smith"]),
    ("authors", ["Alice", "Alice"], ["Alice"]),
    ("title", "1-2 GHz", "12 GHz"),
    ("abstract", "A+B", "AB"),
    ("publication_date", "2020-01-01", "D:20200101000000"),
])
def test_substantive_differences_remain_distinct(field, left, right):
    assert not policy.equivalent_values(field, left, right)


def test_unknown_sources_and_unresolved_shorter_author_lists_are_not_hidden():
    assert policy.needs_review("title", "Current", "Other", None, "ieee_api")
    assert policy.needs_review("authors", ["Alice", "Bob"], ["Alice"], "ieee_api", "arxiv_api")
    assert policy.needs_review("authors", ["Alice", "Bob"], ["Alice"], "ieee_api", "arxiv_api", resolved_by_rule=True)


def test_pdf_name_variants_corroborate_spelling_without_becoming_equivalent():
    left, right = ["Tomislav Matic", "Ann-Marie Smith"], ["Tomislav Matić", "Ann Marie Smith"]
    assert policy.byline_spelling_variants(left, right)
    assert not policy.equivalent_values("authors", left, right)
    assert not policy.byline_spelling_variants(left, list(reversed(right)))
    assert not policy.byline_spelling_variants(["A. Smith"], ["Alice Smith"])
    assert not policy.byline_spelling_variants(["Alice", "Alice"], ["Alice"])


@pytest.mark.parametrize("complete,expected", [(True, "current"), (False, "review")])
def test_only_a_bounded_body_abstract_resolves_an_old_prefix(complete, expected):
    full = "This paper presents a converter in 65 nm CMOS. It reduces power."
    evidence = {"text_state": "ok", "abstract": full, "abstract_complete": complete}
    assert policy.pdf_choice("abstract", full, "This paper presents a converter in",
                             "pdf_heuristic", "pdf_heuristic", evidence=evidence)[0] == expected
