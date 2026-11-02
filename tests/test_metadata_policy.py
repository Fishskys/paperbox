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
    ("authors", ["Changqing. Feng", "Shubin Liu", "Qi An"], ["Changqing Feng", "Shubin Liu", "Qi An"]),
    ("authors", ["Gabor. C. Temes"], ["Gabor C. Temes"]),
    ("authors", ["Alice Smith."], ["Alice Smith"]),
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
    ("authors", ["Y. Arai"], ["Y. Ami'"]),
    ("authors", ["A. Smith"], ["A Smith"]),
    ("authors", ["Changqing.Feng"], ["Changqing Feng"]),
    ("authors", ["Changqing.. Feng"], ["Changqing Feng"]),
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


def test_full_name_word_periods_do_not_require_pdf_boundary_evidence_or_rewrite_values():
    kept = ["Changqing. Feng", "Shubin Liu", "Qi An"]
    rejected = ["Changqing Feng", "Shubin Liu", "Qi An"]
    assert not policy.needs_review("authors", kept, rejected, "pdf_heuristic", "pdf_embedded",
                                   evidence={"byline_complete": False})
    assert kept[0] == "Changqing. Feng"
    assert rejected[0] == "Changqing Feng"


@pytest.mark.parametrize("short,full", [
    (["G. Avolio"], ["Giuseppe Avolio"]),
    (["P.M. Levine", "G.W. Roberts"], ["Peter M. Levine", "Gordon W. Roberts"]),
    (["J.-P. Jansson"], ["Jussi-Pekka Jansson"]),
    (["J. L. Cura"], ["José L. Cura"]),
])
def test_pdf_initials_need_complete_same_position_byline(short, full):
    assert policy.byline_abbreviations(short, full)
    assert not policy.equivalent_values("authors", short, full)
    for complete in (False, True):
        evidence = {"text_state": "ok", "byline": full, "byline_complete": complete}
        assert policy.needs_review("authors", short, full, "pdf_embedded", "pdf_heuristic",
                                   evidence=evidence) is (not complete)
    assert policy.needs_review("authors", short, full, "pdf_embedded", "pdf_heuristic")


@pytest.mark.parametrize("left,right", [
    (["Giovanni Avolio"], ["Giuseppe Avolio"]),
    (["G. Avolio"], ["Peter Avolio"]),
    (["G. Avolio"], ["Giuseppe Avorio"]),
    (["G. Avolio"], ["Giuseppe A. Avolio"]),
    (["G. Avolio", "P. Smith"], ["Peter Smith", "Giuseppe Avolio"]),
    (["G. Avolio", "G. Avolio"], ["Giuseppe Avolio"]),
    (["G. Avolio", "G. Avolio"], ["Giuseppe Avolio", "Giovanni Avolio"]),
    (["G. Avolio", "Ann-Marie Smith"], ["Giuseppe Avolio", "Ann Marie Smith"]),
    (["G. Avolio", "Peter Smith'"], ["Giuseppe Avolio", "Peter Smith"]),
    (["A. Ann-Smith"], ["Alice Ann Smith"]),
    (["J. Peter-Paul Smith"], ["John Peter Paul Smith"]),
    (["P. Palojarvi"], ["Pasi Palojärvi"]),
    (["A"], ["Alice"]),
])
def test_pdf_initial_matching_preserves_identity_differences(left, right):
    assert not policy.byline_abbreviations(left, right)


def test_initials_in_the_pdf_cannot_prove_a_proposed_full_name():
    evidence = {"text_state": "ok", "byline": ["G. Avolio"], "byline_complete": True}
    assert policy.needs_review("authors", ["G. Avolio"], ["Giuseppe Avolio"],
        "pdf_embedded", "pdf_heuristic", evidence=evidence)
    assert policy.needs_review("authors", ["Giovanni Avolio"], ["Giuseppe Avolio"],
        "pdf_embedded", "pdf_heuristic", evidence=evidence)


def test_replacing_invalid_authors_uses_the_actual_full_byline():
    evidence = {"text_state": "ok", "byline": ["Giuseppe Avolio"], "byline_complete": True}
    assert policy.pdf_choice("authors", ["RANGE SENSORS"], ["G. Avolio"], "pdf_embedded", "pdf_embedded",
                             evidence=evidence)[0] == "review"
    assert policy.pdf_choice("authors", ["RANGE SENSORS"], ["Giuseppe Avolio"], "pdf_embedded", "pdf_heuristic",
                             evidence=evidence)[0] == "incoming"


@pytest.mark.parametrize("complete,expected", [(True, "current"), (False, "review")])
def test_only_a_bounded_body_abstract_resolves_an_old_prefix(complete, expected):
    full = "This paper presents a converter in 65 nm CMOS. It reduces power."
    evidence = {"text_state": "ok", "abstract": full, "abstract_complete": complete}
    assert policy.pdf_choice("abstract", full, "This paper presents a converter in",
                             "pdf_heuristic", "pdf_heuristic", evidence=evidence)[0] == expected
