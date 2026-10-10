"""Field-level provenance: one current value per field, history kept, rollback works."""

from __future__ import annotations

import pytest

from app.db.models import Paper, PaperFieldProvenance, PaperIdentifier, PaperSource, new_uuid
from app.services import metadata_identifiers as ids
from app.services import metadata_tags as tags
from app.services import provenance_service as prov


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "Untitled",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "PENDING",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def make_source(session, paper_id=None, source_type="ieee_api", ref=None) -> PaperSource:
    source = PaperSource(
        id=new_uuid(),
        paper_id=paper_id,
        source_type=source_type,
        source_ref=ref or f"doi:{new_uuid()}",
        raw={"test": True},
        match_status="matched",
    )
    session.add(source)
    session.flush()
    return source


def test_a_structural_correction_replaces_the_identifier_and_the_mirror(db_session) -> None:  # noqa: ANN001
    """``override=True`` means the previous value is wrong (AGENTS §3.9).

    Two things have to follow, or the paper keeps an identity nobody believes in:
    the identifier row of that scheme (a second row would stay ``is_primary``,
    because the oldest row of a scheme wins) and the mirrored column.
    """
    paper = make_paper(db_session)
    weak = make_source(db_session, paper.id, source_type="filename")
    structured = make_source(db_session, paper.id, source_type="pdf_embedded")

    prov.set_field(db_session, paper, "identifier:arxiv", "2105.11453", source_id=weak.id)
    assert paper.arxiv_id == "2105.11453"

    prov.set_field(
        db_session,
        paper,
        "identifier:arxiv",
        "2105.11499",
        source_id=structured.id,
        override=True,
    )
    db_session.flush()

    rows = [
        row.normalized_value
        for row in db_session.query(PaperIdentifier).filter_by(paper_id=paper.id, scheme="arxiv")
    ]
    assert rows == ["2105.11499"]  # the rejected value is gone, not kept alongside
    assert paper.arxiv_id == "2105.11499"
    winner = ids.primary_identifier(ids.identifiers_for_paper(db_session, paper.id))
    assert winner is not None and winner.normalized_value == "2105.11499"


# --------------------------------------------------------------------------- #
# field naming
# --------------------------------------------------------------------------- #
def test_identifier_and_tag_fields_are_namespaced() -> None:
    assert prov.identifier_field("doi") == "identifier:doi"
    assert prov.identifier_field("IEEE_Article_Number") == "identifier:ieee_article_number"
    assert prov.tag_field("IEEE_TERMS") == "tag:ieee_terms"


def test_field_names_round_trip() -> None:
    assert prov.scheme_of_identifier_field("identifier:arxiv") == "arxiv"
    assert prov.scheme_of_identifier_field("title") is None
    assert prov.kind_of_tag_field("tag:author_terms") == "author_terms"
    assert prov.kind_of_tag_field("abstract") is None


def test_known_fields_cover_identifiers_and_tags() -> None:
    fields = prov.known_fields()

    assert "title" in fields and "venue" in fields and "authors" in fields
    assert "identifier:doi" in fields
    assert "tag:ieee_terms" in fields
    assert prov.is_known_field("identifier:doi") is True
    assert prov.is_known_field("tag:ieee_terms") is True
    assert prov.is_known_field("nonsense") is False


# --------------------------------------------------------------------------- #
# record / current
# --------------------------------------------------------------------------- #
def test_set_field_writes_the_column_and_the_claim(db_session) -> None:
    paper = make_paper(db_session)

    claim = prov.set_field(db_session, paper, prov.FIELD_TITLE, "Real Title")

    assert paper.title == "Real Title"
    assert claim.is_current is True
    assert claim.decided_by == prov.DECIDED_INITIAL
    assert prov.current_claim(db_session, paper.id, "title").id == claim.id


def test_a_second_claim_demotes_the_first_but_keeps_it(db_session) -> None:
    paper = make_paper(db_session)
    first = prov.set_field(db_session, paper, prov.FIELD_TITLE, "First")
    second = prov.set_field(db_session, paper, prov.FIELD_TITLE, "Second")

    history = prov.field_history(db_session, paper.id, "title")
    current_rows = [row for row in history if row.is_current]

    assert paper.title == "Second"
    assert len(history) == 2
    assert [row.id for row in current_rows] == [second.id]
    assert first.is_current is False
    assert first.value == "First", "history is append-only"


def test_each_field_has_its_own_current_value(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(db_session, paper, "title", "A")
    prov.set_field(db_session, paper, "year", 2015)

    assert prov.current_claim(db_session, paper.id, "title").value == "A"
    assert prov.current_claim(db_session, paper.id, "year").value == 2015


def test_restating_the_same_value_does_not_add_a_row(db_session) -> None:
    paper = make_paper(db_session)
    first = prov.set_field(db_session, paper, "year", 2015)

    again = prov.set_field(db_session, paper, "year", 2015)

    assert again.id == first.id
    assert len(prov.field_history(db_session, paper.id, "year")) == 1


def test_record_claim_can_keep_a_losing_value_out_of_the_columns(db_session) -> None:
    """The merge engine records conflicts without publishing them."""
    paper = make_paper(db_session, title="Heuristic title")
    prov.set_field(db_session, paper, "title", "Heuristic title")

    loser = prov.record_claim(
        db_session, paper_id=paper.id, field="title", value="Other title"
    )

    assert loser.is_current is False
    assert paper.title == "Heuristic title"
    assert len(prov.field_history(db_session, paper.id, "title")) == 2


def test_an_override_is_not_a_conflict(db_session) -> None:
    """Rule 2 replacing a heuristic value is expected, so it is not on the list."""
    paper = make_paper(db_session)
    heuristic = make_source(db_session, paper.id, "pdf_heuristic")
    structured = make_source(db_session, paper.id, "ieee_api")
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="1",
        source_id=heuristic.id,
        decided_by="initial",
        make_current=True,
    )
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="62",
        source_id=structured.id,
        decided_by="structured_override",
        make_current=True,
    )

    assert prov.recorded_conflicts(db_session) == []


def test_two_structured_sources_disagreeing_is_a_conflict(db_session) -> None:
    paper = make_paper(db_session)
    first = make_source(db_session, paper.id, "ieee_api")
    second = make_source(db_session, paper.id, "import_file")
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="62",
        source_id=first.id,
        decided_by="initial",
        make_current=True,
    )
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="63",
        source_id=second.id,
        decided_by="conflict",
        make_current=False,
    )

    conflicts = prov.recorded_conflicts(db_session)
    assert [row["field"] for row in conflicts] == ["volume"]
    assert conflicts[0]["kept"] == "62" and conflicts[0]["rejected"] == "63"


def test_claims_carry_their_source_and_confidence(db_session) -> None:
    paper = make_paper(db_session)
    source = make_source(db_session, paper.id)

    claim = prov.set_field(
        db_session,
        paper,
        "abstract",
        "Abstract text",
        source_id=source.id,
        confidence=1.0,
        decided_by=prov.DECIDED_STRUCTURED_OVERRIDE,
    )

    assert claim.source_id == source.id
    assert claim.confidence == 1.0
    assert claim.decided_by == prov.DECIDED_STRUCTURED_OVERRIDE


def test_record_claim_rejects_an_empty_field_name(db_session) -> None:
    paper = make_paper(db_session)

    assert prov.record_claim(db_session, paper_id=paper.id, field="", value="x") is None


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #
def test_simple_fields_land_on_their_column(db_session) -> None:
    paper = make_paper(db_session)

    for field, value in (
        ("abstract", "Abstract"),
        ("language", "en"),
        ("year", 2015),
        ("volume", "62"),
        ("issue", "7"),
        ("pages", "631-635"),
        ("paper_type", "journal"),
        ("url", "https://example.org/x"),
    ):
        prov.set_field(db_session, paper, field, value)

    assert paper.abstract == "Abstract"
    assert paper.language == "en"
    assert paper.year == 2015
    assert (paper.volume, paper.issue, paper.pages) == ("62", "7", "631-635")
    assert paper.paper_type == "journal"
    assert paper.url == "https://example.org/x"


def test_publication_date_accepts_ieee_month_precision(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(db_session, paper, "publication_date", "July 2015")

    assert paper.publication_date.isoformat() == "2015-07-01"


def test_a_blank_title_is_never_written(db_session) -> None:
    paper = make_paper(db_session, title="Kept")

    prov.write_field(db_session, paper, "title", "   ")

    assert paper.title == "Kept"


def test_venue_claim_creates_venue_and_edition(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(
        db_session,
        paper,
        prov.FIELD_VENUE,
        {
            "name": "IEEE International Solid-State Circuits Conference",
            "year": 2015,
            "content_type": "Conferences",
            "location": "San Francisco, CA, USA",
        },
    )

    assert paper.venue is not None
    assert paper.venue.name == "IEEE International Solid-State Circuits Conference"
    assert paper.venue_year == 2015
    assert paper.venue_edition is not None
    assert paper.venue_edition.location == "San Francisco, CA, USA"


def test_authors_claim_rewrites_the_author_list(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(db_session, paper, prov.FIELD_AUTHORS, ["Alice", "Bob"])
    assert prov.read_field(paper, prov.FIELD_AUTHORS) == ["Alice", "Bob"]

    prov.set_field(db_session, paper, prov.FIELD_AUTHORS, ["Carol"])
    assert prov.read_field(paper, prov.FIELD_AUTHORS) == ["Carol"]


def test_identifier_claim_updates_the_table_and_the_mirror(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(db_session, paper, "identifier:doi", "10.1109/JSSC.2020.1234567")

    assert paper.doi == "10.1109/jssc.2020.1234567"
    rows = ids.identifiers_for_paper(db_session, paper.id)
    assert [row.scheme for row in rows] == ["doi"]
    assert rows[0].is_primary is True


def test_tag_claim_links_with_the_requested_kind(db_session) -> None:
    paper = make_paper(db_session)

    prov.set_field(db_session, paper, "tag:ieee_terms", ["SRAM", "Leakage"])
    prov.set_field(db_session, paper, "tag:author_terms", ["Low power"])

    assert tags.tags_for_paper(db_session, paper, kind="ieee_terms") == ["Leakage", "SRAM"]
    assert tags.tags_for_paper(db_session, paper, kind="author_terms") == ["Low power"]
    assert tags.tags_for_paper(db_session, paper) == ["Leakage", "Low power", "SRAM"]


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def test_rollback_restores_the_previous_value(db_session) -> None:
    paper = make_paper(db_session)
    first = prov.set_field(db_session, paper, "title", "First")
    prov.set_field(db_session, paper, "title", "Second")

    prov.rollback_field(db_session, paper, "title", first.id)

    assert paper.title == "First"
    assert prov.current_claim(db_session, paper.id, "title").id == first.id
    assert len(prov.field_history(db_session, paper.id, "title")) == 2, "history survives"


def test_rollback_of_a_venue_claim_re_points_the_paper(db_session) -> None:
    paper = make_paper(db_session)
    first = prov.set_field(db_session, paper, prov.FIELD_VENUE, {"name": "ISSCC", "year": 2014})
    prov.set_field(db_session, paper, prov.FIELD_VENUE, {"name": "ISSCC", "year": 2015})

    # Filling blanks is the merge rule, so the merge path kept 2014...
    prov.write_field(db_session, paper, prov.FIELD_VENUE, {"name": "ISSCC", "year": 2015})
    assert paper.venue_year == 2014
    # ...while an explicit override (manual edit) really moves the edition.
    prov.write_field(
        db_session, paper, prov.FIELD_VENUE, {"name": "ISSCC", "year": 2015}, override=True
    )
    assert paper.venue_year == 2015

    prov.rollback_field(db_session, paper, prov.FIELD_VENUE, first.id)

    assert paper.venue_year == 2014


def test_rollback_of_an_authors_claim_restores_the_list(db_session) -> None:
    paper = make_paper(db_session)
    first = prov.set_field(db_session, paper, prov.FIELD_AUTHORS, ["Alice", "Bob"])
    prov.set_field(db_session, paper, prov.FIELD_AUTHORS, ["Carol"])

    prov.rollback_field(db_session, paper, prov.FIELD_AUTHORS, first.id)

    assert prov.read_field(paper, prov.FIELD_AUTHORS) == ["Alice", "Bob"]


def test_rollback_refuses_a_claim_of_another_paper(db_session) -> None:
    paper = make_paper(db_session)
    other = make_paper(db_session, title="Other")
    claim = prov.set_field(db_session, other, "title", "Other title")

    with pytest.raises(LookupError):
        prov.rollback_field(db_session, paper, "title", claim.id)


def test_rollback_refuses_a_claim_of_another_field(db_session) -> None:
    paper = make_paper(db_session)
    claim = prov.set_field(db_session, paper, "abstract", "Some abstract")

    with pytest.raises(LookupError):
        prov.rollback_field(db_session, paper, "title", claim.id)


def test_rollback_of_an_unknown_id_raises(db_session) -> None:
    paper = make_paper(db_session)

    with pytest.raises(LookupError):
        prov.rollback_field(db_session, paper, "title", new_uuid())


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #
def test_provenance_summary_reports_history_and_current(db_session) -> None:
    paper = make_paper(db_session)
    source = make_source(db_session, paper.id)
    prov.set_field(db_session, paper, "title", "First")
    prov.set_field(
        db_session, paper, "title", "Second", source_id=source.id, confidence=0.8
    )

    summary = prov.provenance_summary(db_session, paper.id)

    entries = summary["title"]
    assert len(entries) == 2
    current = [entry for entry in entries if entry["is_current"]]
    assert len(current) == 1
    assert current[0]["value"] == "Second"
    assert current[0]["source_id"] == source.id
    assert current[0]["confidence"] == 0.8
    assert {entry["decided_by"] for entry in entries} == {prov.DECIDED_INITIAL}


def test_provenance_for_paper_groups_by_field(db_session) -> None:
    paper = make_paper(db_session)
    prov.set_field(db_session, paper, "title", "T")
    prov.set_field(db_session, paper, "year", 2015)

    grouped = prov.provenance_for_paper(db_session, paper.id)

    assert set(grouped) == {"title", "year"}
    assert all(rows[0].is_current for rows in grouped.values())


def test_current_claim_is_none_before_anything_is_recorded(db_session) -> None:
    paper = make_paper(db_session)

    assert prov.current_claim(db_session, paper.id, "title") is None
    assert prov.field_history(db_session, paper.id) == []
    assert db_session.query(PaperFieldProvenance).count() == 0


def test_read_field_mirrors_the_column(db_session) -> None:
    paper = make_paper(db_session, year=2015, doi="10.1/x")

    assert prov.read_field(paper, "year") == 2015
    assert prov.read_field(paper, "identifier:doi") == "10.1/x"
    assert prov.read_field(paper, "venue") is None

def test_a_conflict_carries_the_losing_provenance_id(db_session) -> None:
    """界面靠这个 id 直接调 rollback「采纳被拒值」，不用再查一次（2026-10-10）。"""
    paper = make_paper(db_session)
    first = make_source(db_session, paper.id, "ieee_api")
    second = make_source(db_session, paper.id, "import_file")
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="62",
        source_id=first.id,
        decided_by="initial",
        make_current=True,
    )
    losing = prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="volume",
        value="63",
        source_id=second.id,
        decided_by="conflict",
        make_current=False,
    )

    conflicts = prov.recorded_conflicts(db_session)

    assert conflicts[0]["provenance_id"] == losing.id


def test_a_field_a_human_edited_drops_off_the_list(db_session) -> None:
    """人工改过这一格之后，旧值不再算"待裁决的分歧"。

    注意「保留现值」不走这条路：``record_claim`` 对"重复当前值"是空操作（防重复导入灌账本），
    所以那条走 ``decided_by='dismissed'`` 的显式裁决，见 test_manual_metadata.py。
    """
    paper = make_paper(db_session)
    structured = make_source(db_session, paper.id, "pdf_embedded")
    human = make_source(db_session, paper.id, "manual")
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="authors",
        value=["Zhuocheng Zhang"],
        source_id=structured.id,
        decided_by="initial",
        make_current=True,
    )
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="authors",
        value=["msi"],
        source_id=structured.id,
        decided_by="conflict",
        make_current=False,
    )
    assert len(prov.recorded_conflicts(db_session)) == 1, "先确认它本来是一处冲突"

    # 人工把这一格改成别的值：现值来源变成 manual，旧值不该再挂成分歧
    prov.set_field(
        db_session,
        paper,
        "authors",
        ["Zhuocheng Zhang", "Lei Wang"],
        source_id=human.id,
        decided_by="manual",
        override=True,
    )

    assert prov.recorded_conflicts(db_session) == []


def test_an_earlier_manual_edit_is_not_an_open_dispute(db_session) -> None:
    """人工改了两次、旧值落败：那是历史，不是待裁决的分歧。"""
    paper = make_paper(db_session)
    human = make_source(db_session, paper.id, "manual")
    other = make_source(db_session, paper.id, "import_file")
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="year",
        value=2023,
        source_id=human.id,
        decided_by="manual",
        make_current=False,
    )
    prov.record_claim(
        db_session,
        paper_id=paper.id,
        field="year",
        value=2024,
        source_id=other.id,
        decided_by="initial",
        make_current=True,
    )

    assert prov.recorded_conflicts(db_session) == []
