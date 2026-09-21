"""Matching an external record against the library (section 7 of the design).

Step 1-3 may attach automatically; a bare title match must not (no UI to confirm,
and a wrong attachment is worse than a review-queue entry).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import Paper, PaperFile, new_uuid
from app.services import metadata_identifiers as ids
from app.services import metadata_matcher as matcher
from app.services import paper_service


def make_paper(session, *, authors=(), **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "Low Power SRAM Leakage Reduction",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    if authors:
        paper_service.set_paper_authors(session, paper, list(authors))
    session.flush()
    return paper


def attach_file(session, paper, sha256=None, filename="original.pdf") -> PaperFile:
    record = PaperFile(
        id=new_uuid(),
        paper_id=paper.id,
        kind="original",
        object_key=f"papers/{paper.id}/original.pdf",
        bucket="paperbox",
        filename=filename,
        content_type="application/pdf",
        size_bytes=10,
        sha256=sha256,
    )
    session.add(record)
    session.flush()
    return record


def test_doi_identifier_matches_with_full_confidence(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1109/JSSC.2020.1234567"
    )

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(identifiers={ids.SCHEME_DOI: "https://doi.org/10.1109/JSSC.2020.1234567"}),
    )

    assert result.status == matcher.STATUS_MATCHED
    assert result.method == matcher.METHOD_DOI
    assert result.confidence == 1.0
    assert result.paper_id == paper.id


def test_arxiv_identifier_matches(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_ARXIV, value="1710.07153"
    )

    result = matcher.match_record(
        db_session, matcher.MatchInput(identifiers={ids.SCHEME_ARXIV: "arXiv:1710.07153v2"})
    )

    assert result.matched and result.method == matcher.METHOD_ARXIV


def test_ieee_article_number_matches(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session,
        paper_id=paper.id,
        scheme=ids.SCHEME_IEEE_ARTICLE_NUMBER,
        value="7065247",
    )

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(identifiers={ids.SCHEME_IEEE_ARTICLE_NUMBER: "7065247"}),
    )

    assert result.matched and result.method == matcher.METHOD_IEEE_ARTICLE_NUMBER


def test_doi_wins_over_arxiv_when_both_are_present(db_session) -> None:
    doi_paper = make_paper(db_session, title="DOI paper")
    arxiv_paper = make_paper(db_session, title="arXiv paper")
    ids.upsert_identifier(
        db_session, paper_id=doi_paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    ids.upsert_identifier(
        db_session, paper_id=arxiv_paper.id, scheme=ids.SCHEME_ARXIV, value="1710.07153"
    )

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(identifiers={ids.SCHEME_ARXIV: "1710.07153", ids.SCHEME_DOI: "10.1/x"}),
    )

    assert result.paper_id == doi_paper.id
    assert result.method == matcher.METHOD_DOI


def test_an_identifier_of_a_deleted_paper_is_a_miss(db_session) -> None:
    paper = make_paper(db_session, deleted_at=datetime.now(timezone.utc))
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )

    result = matcher.match_record(
        db_session, matcher.MatchInput(identifiers={ids.SCHEME_DOI: "10.1/x"})
    )

    assert result.status == matcher.STATUS_UNMATCHED


def test_sha256_matches_an_already_stored_file(db_session) -> None:
    digest = "a" * 64
    paper = make_paper(db_session)
    attach_file(db_session, paper, sha256=digest)

    result = matcher.match_record(db_session, matcher.MatchInput(sha256=digest.upper()))

    assert result.matched and result.method == matcher.METHOD_SHA256


def test_a_short_digest_is_not_a_sha256(db_session) -> None:
    paper = make_paper(db_session)
    attach_file(db_session, paper, sha256="a" * 64)

    result = matcher.match_record(db_session, matcher.MatchInput(sha256="abc"))

    assert result.status == matcher.STATUS_UNMATCHED


def test_title_author_and_year_together_attach(db_session) -> None:
    paper = make_paper(db_session, year=2015, authors=["Alice Smith", "Bob Jones"])

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(
            title="Low   Power SRAM Leakage Reduction!",
            authors=["alice smith"],
            year=2015,
        ),
    )

    assert result.matched
    assert result.method == matcher.METHOD_TITLE_YEAR_AUTHOR
    assert result.confidence == 0.8
    assert result.paper_id == paper.id


def test_title_only_is_ambiguous_and_never_attaches(db_session) -> None:
    paper = make_paper(db_session, year=2015, authors=["Alice Smith"])

    result = matcher.match_record(
        db_session, matcher.MatchInput(title="Low Power SRAM Leakage Reduction")
    )

    assert result.status == matcher.STATUS_AMBIGUOUS
    assert result.method == matcher.METHOD_TITLE
    assert result.confidence == 0.5
    assert result.paper is None
    assert [item["paper_id"] for item in result.candidates] == [paper.id]


def test_title_with_author_but_no_year_is_still_ambiguous(db_session) -> None:
    paper = make_paper(db_session, year=None, authors=["Alice Smith"])

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(title="Low Power SRAM Leakage Reduction", authors=["Alice Smith"]),
    )

    assert result.status == matcher.STATUS_AMBIGUOUS
    assert result.paper is None
    assert result.candidates[0]["author_match"] is True


def test_a_different_author_keeps_the_match_ambiguous(db_session) -> None:
    make_paper(db_session, year=2015, authors=["Alice Smith"])

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(
            title="Low Power SRAM Leakage Reduction", authors=["Someone Else"], year=2015
        ),
    )

    assert result.status == matcher.STATUS_AMBIGUOUS
    assert result.candidates[0]["author_match"] is False


def test_a_different_title_does_not_match(db_session) -> None:
    make_paper(db_session, title="Completely unrelated work")

    result = matcher.match_record(
        db_session,
        matcher.MatchInput(title="Low Power SRAM Leakage Reduction", year=2015),
    )

    assert result.status == matcher.STATUS_UNMATCHED


def test_filename_is_the_last_weak_signal(db_session) -> None:
    paper = make_paper(db_session, title="Something else entirely")
    attach_file(db_session, paper, filename="low_power_sram.pdf")

    result = matcher.match_record(
        db_session, matcher.MatchInput(title="Unknown title", filename="Low Power SRAM.pdf")
    )

    assert result.status == matcher.STATUS_AMBIGUOUS
    assert result.method == matcher.METHOD_FILENAME
    assert [item["paper_id"] for item in result.candidates] == [paper.id]


def test_nothing_matches_on_an_empty_library(db_session) -> None:
    result = matcher.match_record(
        db_session, matcher.MatchInput(title="Nothing here", year=2015)
    )

    assert result.status == matcher.STATUS_UNMATCHED
    assert result.paper_id is None
    assert result.as_dict()["match_method"] is None


def test_match_input_is_built_from_claim_values() -> None:
    match_input = matcher.match_input_from_values(
        {
            "title": "A title",
            "authors": ["Alice"],
            "identifier:doi": "10.1/x",
            "identifier:ieee_article_number": "7065247",
            "venue": {"name": "ISSCC", "year": 2015},
        },
        source_ref="file:/tmp/a.pdf:" + "b" * 64,
        filename="a.pdf",
    )

    assert match_input.identifiers == {
        "doi": "10.1/x",
        "ieee_article_number": "7065247",
    }
    assert match_input.year == 2015
    assert match_input.sha256 == "b" * 64
    assert match_input.first_author() == "Alice"


def test_match_input_ignores_a_source_ref_without_a_digest() -> None:
    assert matcher._sha256_from_ref("doi:10.1/x") is None
    assert matcher._sha256_from_ref(None) is None


def test_title_candidates_compare_normalized_titles(db_session) -> None:
    paper = make_paper(db_session, title="Low-Power SRAM: Leakage Reduction")

    found = matcher.title_candidates(db_session, "low power sram leakage reduction")

    assert [item.id for item in found] == [paper.id]
    assert matcher.title_candidates(db_session, None) == []


def test_soft_deleted_papers_are_never_candidates(db_session) -> None:
    make_paper(
        db_session,
        title="Low Power SRAM Leakage Reduction",
        deleted_at=datetime.now(timezone.utc),
    )

    assert matcher.title_candidates(db_session, "Low Power SRAM Leakage Reduction") == []