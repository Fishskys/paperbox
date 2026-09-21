"""Identifier normalization, primary derivation and idempotent writes.

Covers decision 2 (``DOI > arXiv > title+first author+year > sha256``) and
decision 6 (the IEEE ``article_number`` lives in ``paper_identifiers``).
"""

from __future__ import annotations

import pytest  # noqa: F401 - fixtures come from conftest

from app.db.models import Paper, new_uuid
from app.services import metadata_identifiers as ids
from app.services import paper_service, provenance_service


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "Low Power SRAM Leakage Reduction",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "PENDING",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


# --------------------------------------------------------------------------- #
# normalization (pure)
# --------------------------------------------------------------------------- #
def test_doi_normalization_matches_the_fingerprint_helper() -> None:
    for raw in (
        "10.1109/JSSC.2020.1234567",
        "  10.1109/jssc.2020.1234567 ",
        "https://doi.org/10.1109/JSSC.2020.1234567",
        "doi:10.1109/JSSC.2020.1234567",
    ):
        assert ids.normalize_identifier(ids.SCHEME_DOI, raw) == "10.1109/jssc.2020.1234567"


def test_doi_normalization_uses_paper_service_exactly() -> None:
    raw = "HTTPS://DOI.ORG/10.1109/JSSC.2020.1234567"

    assert ids.normalize_identifier(ids.SCHEME_DOI, raw) == paper_service.normalize_doi(raw)


def test_arxiv_normalization_drops_prefix_and_version() -> None:
    for raw in ("1710.07153", "1710.07153v2", "arXiv:1710.07153", "https://arxiv.org/abs/1710.07153"):
        assert ids.normalize_identifier(ids.SCHEME_ARXIV, raw) == "1710.07153"


def test_ieee_article_number_keeps_only_digits() -> None:
    assert ids.normalize_identifier(ids.SCHEME_IEEE_ARTICLE_NUMBER, 7065247) == "7065247"
    assert ids.normalize_identifier(ids.SCHEME_IEEE_ARTICLE_NUMBER, " 7065247.0 ") == "70652470"


def test_issn_drops_the_hyphen_and_upper_cases_the_check_digit() -> None:
    assert ids.normalize_identifier(ids.SCHEME_ISSN, "0018-9219") == "00189219"
    assert ids.normalize_identifier(ids.SCHEME_ISSN, "0018-921x") == "0018921X"


def test_sha256_only_accepts_a_real_digest() -> None:
    digest = "a" * 64

    assert ids.normalize_identifier(ids.SCHEME_SHA256, digest.upper()) == digest
    assert ids.normalize_identifier(ids.SCHEME_SHA256, "not-a-digest") is None


def test_normalization_rejects_blank_values() -> None:
    for value in (None, "", "   "):
        assert ids.normalize_identifier(ids.SCHEME_DOI, value) is None


def test_unknown_scheme_still_gets_a_stable_normalization() -> None:
    assert ids.normalize_identifier("openreview", "  Foo Bar  ") == "foo bar"


# --------------------------------------------------------------------------- #
# primary derivation (pure)
# --------------------------------------------------------------------------- #
def rows(*pairs) -> list[dict]:
    return [
        {"scheme": scheme, "normalized_value": value, "id": f"{scheme}-row"}
        for scheme, value in pairs
    ]


def test_doi_wins_as_the_primary_identifier() -> None:
    winner = ids.primary_identifier(
        rows(
            (ids.SCHEME_IEEE_ARTICLE_NUMBER, "7065247"),
            (ids.SCHEME_ARXIV, "1710.07153"),
            (ids.SCHEME_DOI, "10.1109/jssc.2020.1"),
        )
    )

    assert winner["scheme"] == ids.SCHEME_DOI


def test_arxiv_is_the_fallback_when_there_is_no_doi() -> None:
    winner = ids.primary_identifier(
        rows((ids.SCHEME_ISSN, "00189219"), (ids.SCHEME_ARXIV, "1710.07153"))
    )

    assert winner["scheme"] == ids.SCHEME_ARXIV


def test_an_ieee_number_alone_is_never_primary() -> None:
    assert ids.primary_identifier(rows((ids.SCHEME_IEEE_ARTICLE_NUMBER, "7065247"))) is None
    assert ids.primary_identifier([]) is None


def test_fingerprint_comes_from_the_primary_identifier() -> None:
    assert (
        ids.identifier_fingerprint(rows((ids.SCHEME_DOI, "10.1109/jssc.2020.1")))
        == "doi:10.1109/jssc.2020.1"
    )
    assert (
        ids.identifier_fingerprint(rows((ids.SCHEME_ARXIV, "1710.07153")))
        == "arxiv:1710.07153"
    )


def test_fingerprint_falls_back_to_the_legacy_ladder() -> None:
    fingerprint = ids.build_fingerprint_from_identifiers(
        rows((ids.SCHEME_IEEE_ARTICLE_NUMBER, "7065247")),
        title="Low Power SRAM",
        first_author="Alice",
        year=2021,
        sha256="a" * 64,
    )

    assert fingerprint == "title:low power sram|alice|2021"


def test_fingerprint_falls_back_to_sha256_without_title_signals() -> None:
    digest = "b" * 64

    assert (
        ids.build_fingerprint_from_identifiers([], sha256=digest) == f"sha256:{digest}"
    )


def test_fingerprint_prefers_the_identifier_over_the_title_ladder() -> None:
    fingerprint = ids.build_fingerprint_from_identifiers(
        rows((ids.SCHEME_DOI, "10.1109/jssc.2020.1")),
        title="Something else entirely",
        first_author="Bob",
        year=1999,
        sha256="c" * 64,
    )

    assert fingerprint == "doi:10.1109/jssc.2020.1"


# --------------------------------------------------------------------------- #
# database helpers
# --------------------------------------------------------------------------- #
def test_upsert_stores_raw_and_normalized_values(db_session) -> None:
    paper = make_paper(db_session)

    row = ids.upsert_identifier(
        db_session,
        paper_id=paper.id,
        scheme=ids.SCHEME_DOI,
        value="https://doi.org/10.1109/JSSC.2020.1234567",
    )

    assert row.value == "https://doi.org/10.1109/JSSC.2020.1234567"
    assert row.normalized_value == "10.1109/jssc.2020.1234567"
    assert row.is_primary is False, "upsert never sets the flag; refresh_primary does"


def test_upsert_is_idempotent_for_the_same_value(db_session) -> None:
    paper = make_paper(db_session)

    first = ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    second = ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="https://doi.org/10.1/X"
    )

    assert first.id == second.id
    assert len(ids.identifiers_for_paper(db_session, paper.id)) == 1


def test_upsert_refuses_to_move_an_identifier_to_another_paper(db_session) -> None:
    """The unique index is the arbiter; the helper must not fight it."""
    first = make_paper(db_session)
    second = make_paper(db_session, title="Another paper")
    existing = ids.upsert_identifier(
        db_session, paper_id=first.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )

    returned = ids.upsert_identifier(
        db_session, paper_id=second.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )

    assert returned.id == existing.id
    assert returned.paper_id == first.id
    assert ids.identifiers_for_paper(db_session, second.id) == []


def test_upsert_reclaims_an_identifier_from_a_deleted_paper(db_session) -> None:
    """A soft-deleted paper must not keep its DOI out of circulation."""
    from datetime import datetime, timezone

    first = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=first.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    first.deleted_at = datetime.now(timezone.utc)
    db_session.flush()
    second = make_paper(db_session, title="Re-ingested")

    row = ids.upsert_identifier(
        db_session, paper_id=second.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )

    assert row.paper_id == second.id
    assert [
        item.paper_id for item in ids.identifiers_for_paper(db_session, second.id)
    ] == [second.id]


def test_soft_delete_releases_the_identifiers(db_session) -> None:
    """Deleting a paper frees its DOI for a future ingest (like the fingerprint)."""
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_SHA256, value="a" * 64
    )

    paper_service.soft_delete_paper(db_session, paper)

    assert ids.identifiers_for_paper(db_session, paper.id) == []
    assert ids.find_identifier(db_session, ids.SCHEME_DOI, "10.1/x") is None
    assert paper.status == paper_service.STATUS_DELETED


def test_soft_delete_keeps_the_claim_history(db_session) -> None:
    """The ledger is history, not state: it survives the deletion."""
    paper = make_paper(db_session)
    provenance_service.set_field(db_session, paper, "title", "Kept as history")

    paper_service.soft_delete_paper(db_session, paper)

    assert provenance_service.current_claim(db_session, paper.id, "title") is not None


def test_upsert_ignores_unusable_values(db_session) -> None:
    paper = make_paper(db_session)

    assert ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="  "
    ) is None
    assert ids.identifiers_for_paper(db_session, paper.id) == []


def test_refresh_primary_flags_exactly_one_row(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_ARXIV, value="1710.07153"
    )
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_IEEE_ARTICLE_NUMBER, value="7065247"
    )
    doi = ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )

    winner = ids.refresh_primary(db_session, paper.id)

    assert winner.id == doi.id
    flagged = [
        row.scheme
        for row in ids.identifiers_for_paper(db_session, paper.id)
        if row.is_primary
    ]
    assert flagged == [ids.SCHEME_DOI]


def test_refresh_primary_demotes_the_old_winner_when_a_doi_arrives(db_session) -> None:
    paper = make_paper(db_session)
    arxiv = ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_ARXIV, value="1710.07153"
    )
    ids.refresh_primary(db_session, paper.id)
    assert arxiv.is_primary is True

    doi = ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    ids.refresh_primary(db_session, paper.id)

    assert doi.is_primary is True
    assert arxiv.is_primary is False


def test_refresh_primary_clears_the_flag_when_only_context_remains(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_ISSN, value="0018-9219"
    )

    assert ids.refresh_primary(db_session, paper.id) is None
    assert all(
        row.is_primary is False
        for row in ids.identifiers_for_paper(db_session, paper.id)
    )


def test_find_identifier_matches_only_the_normalized_form(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/ABC"
    )

    assert ids.find_identifier(db_session, ids.SCHEME_DOI, "10.1/abc") is not None
    assert ids.find_identifier(db_session, ids.SCHEME_DOI, "10.1/other") is None
    assert ids.find_identifier(db_session, ids.SCHEME_ARXIV, "10.1/abc") is None
    assert ids.find_identifier(db_session, "", "") is None


def test_mirror_legacy_columns_fills_but_never_blanks(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1109/JSSC.2020.1"
    )
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_ARXIV, value="1710.07153"
    )

    ids.mirror_legacy_columns(db_session, paper)

    assert paper.doi == "10.1109/jssc.2020.1"
    assert paper.arxiv_id == "1710.07153"


def test_mirror_legacy_columns_keeps_an_existing_value(db_session) -> None:
    paper = make_paper(db_session, doi="10.9999/manual")
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1109/jssc.2020.1"
    )

    ids.mirror_legacy_columns(db_session, paper)

    assert paper.doi == "10.9999/manual"


def test_first_source_is_recorded(db_session) -> None:
    from app.db.models import PaperSource

    paper = make_paper(db_session)
    source = PaperSource(
        id=new_uuid(),
        paper_id=paper.id,
        source_type="ieee_api",
        source_ref="doi:10.1/x",
        raw={},
        match_status="matched",
    )
    db_session.add(source)
    db_session.flush()

    row = ids.upsert_identifier(
        db_session,
        paper_id=paper.id,
        scheme=ids.SCHEME_DOI,
        value="10.1/x",
        first_source_id=source.id,
    )

    assert row.first_source_id == source.id