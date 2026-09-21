"""Venue/edition split (decision 7).

The important property: two papers at the same conference in different years
produce **one** ``venues`` row and **two** ``venue_editions`` rows, and both
"the conference" and "the conference + 2015" find exactly the right papers.
"""

from __future__ import annotations

from app.db.models import Paper, Venue, VenueEdition, new_uuid
from app.services import venue_service as venues


def make_paper(session, **overrides) -> Paper:
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
    return paper


# --------------------------------------------------------------------------- #
# pure mapping helpers
# --------------------------------------------------------------------------- #
def test_venue_kind_is_inferred_from_the_ieee_content_type() -> None:
    assert venues.venue_kind_for_content_type("Journals") == venues.KIND_JOURNAL
    assert venues.venue_kind_for_content_type("Conferences") == venues.KIND_CONFERENCE
    assert venues.venue_kind_for_content_type(" standards ") == venues.KIND_STANDARD
    assert venues.venue_kind_for_content_type("Magazines") == venues.KIND_MAGAZINE


def test_unknown_content_type_maps_to_nothing() -> None:
    assert venues.venue_kind_for_content_type(None) is None
    assert venues.venue_kind_for_content_type("Courses") == venues.KIND_UNKNOWN
    assert venues.paper_type_for_content_type("Courses") is None


def test_paper_type_is_inferred_from_the_ieee_content_type() -> None:
    assert venues.paper_type_for_content_type("Journals") == venues.PAPER_TYPE_JOURNAL
    assert venues.paper_type_for_content_type("Conferences") == venues.PAPER_TYPE_CONFERENCE
    assert venues.paper_type_for_content_type("Early Access") == venues.PAPER_TYPE_EARLY_ACCESS


def test_venue_name_normalization_ignores_case_and_punctuation() -> None:
    assert venues.normalize_venue_name("IEEE  Trans. on Circuits—Systems") == (
        venues.normalize_venue_name("ieee trans on circuits systems")
    )
    assert venues.normalize_venue_name(None) == ""


def test_year_parsing_accepts_month_precision_input() -> None:
    assert venues._parse_year(2015) == 2015
    assert venues._parse_year("2015") == 2015
    assert venues._parse_year("July 2015") == 2015
    assert venues._parse_year("15") is None
    assert venues._parse_year(None) is None


# --------------------------------------------------------------------------- #
# venue + edition creation
# --------------------------------------------------------------------------- #
def test_same_venue_two_years_is_one_venue_and_two_editions(db_session) -> None:
    first, first_edition = venues.resolve_venue(
        db_session,
        name="IEEE International Solid-State Circuits Conference",
        year=2014,
        content_type="Conferences",
        location="San Francisco, CA, USA",
        dates="9-13 Feb. 2014",
        publication_number="12345",
    )
    second, second_edition = venues.resolve_venue(
        db_session,
        name="ieee international solid-state circuits conference",
        year=2015,
        content_type="Conferences",
        location="San Francisco, CA, USA",
    )

    assert first.id == second.id, "same conference -> one venues row"
    assert first_edition.id != second_edition.id
    assert db_session.query(Venue).count() == 1
    assert db_session.query(VenueEdition).count() == 2
    assert venues.venue_editions_for_venue(db_session, first.id)[0].year == 2014
    assert first.kind == venues.KIND_CONFERENCE


def test_edition_keeps_the_first_non_empty_value(db_session) -> None:
    venue, edition = venues.resolve_venue(
        db_session, name="ISSCC", year=2015, location="San Francisco"
    )
    _, again = venues.resolve_venue(
        db_session, name="ISSCC", year=2015, location="Somewhere else", dates="1-5 Feb."
    )

    assert again.id == edition.id
    assert again.location == "San Francisco", "fill-blanks only"
    assert again.dates == "1-5 Feb."


def test_venue_creation_can_fill_in_missing_fields_later(db_session) -> None:
    venue = venues.get_or_create_venue(db_session, "IEEE Transactions on Circuits")
    assert venue.kind is None and venue.issn is None

    again = venues.get_or_create_venue(
        db_session, "IEEE Transactions on Circuits", kind="journal", issn="0018-9219"
    )

    assert again.id == venue.id
    assert again.kind == "journal"
    assert again.issn == "0018-9219"


def test_venue_fields_are_never_overwritten(db_session) -> None:
    venues.get_or_create_venue(
        db_session, "ISSCC", kind=venues.KIND_CONFERENCE, issn="0018-9219"
    )

    again = venues.get_or_create_venue(
        db_session, "ISSCC", kind=venues.KIND_JOURNAL, issn="9999-9999"
    )

    assert again.kind == venues.KIND_CONFERENCE
    assert again.issn == "0018-9219"


def test_no_venue_without_a_name_and_no_edition_without_a_year(db_session) -> None:
    assert venues.get_or_create_venue(db_session, "") is None
    venue = venues.get_or_create_venue(db_session, "ISSCC")

    assert venues.get_or_create_edition(db_session, venue, None) is None
    assert venues.get_or_create_edition(db_session, venue, "not a year") is None
    assert db_session.query(VenueEdition).count() == 0


def test_edition_year_is_parsed_from_a_month_string(db_session) -> None:
    _, edition = venues.resolve_venue(db_session, name="ISSCC", year="July 2015")

    assert edition.year == 2015


# --------------------------------------------------------------------------- #
# attaching to papers + search semantics
# --------------------------------------------------------------------------- #
def test_attach_venue_sets_the_redundant_year_column(db_session) -> None:
    paper = make_paper(db_session)
    venue, edition = venues.resolve_venue(
        db_session, name="ISSCC", year=2015, content_type="Conferences"
    )

    assert venues.attach_venue(db_session, paper, venue, edition) is True
    assert paper.venue_id == venue.id
    assert paper.venue_edition_id == edition.id
    assert paper.venue_year == 2015


def test_attach_venue_does_not_re_point_an_existing_venue(db_session) -> None:
    first = venues.get_or_create_venue(db_session, "ISSCC")
    other = venues.get_or_create_venue(db_session, "Journal of Testing")
    paper = make_paper(db_session, venue_id=first.id, venue_year=2014)

    assert venues.attach_venue(db_session, paper, other, None) is False
    assert paper.venue_id == first.id


def test_searching_the_venue_matches_every_year(db_session) -> None:
    papers = []
    for year in (2014, 2015):
        venue, edition = venues.resolve_venue(
            db_session, name="ISSCC", year=year, content_type="Conferences"
        )
        paper = make_paper(db_session, title=f"SRAM {year}")
        venues.attach_venue(db_session, paper, venue, edition)
        papers.append(paper)
    other_venue, other_edition = venues.resolve_venue(
        db_session, name="IEEE JSSC", year=2015, content_type="Journals"
    )
    other = make_paper(db_session, title="Unrelated")
    venues.attach_venue(db_session, other, other_venue, other_edition)

    by_name = venues.find_papers_by_venue(db_session, name="ISSCC")

    assert {paper.id for paper in by_name} == {paper.id for paper in papers}


def test_searching_venue_and_year_narrows_to_one_edition(db_session) -> None:
    for year in (2014, 2015):
        venue, edition = venues.resolve_venue(
            db_session, name="ISSCC", year=year, content_type="Conferences"
        )
        paper = make_paper(db_session, title=f"SRAM {year}")
        venues.attach_venue(db_session, paper, venue, edition)

    narrow = venues.find_papers_by_venue(db_session, name="ISSCC", year=2015)
    by_year_only = venues.find_papers_by_venue(db_session, year=2014)

    assert [paper.title for paper in narrow] == ["SRAM 2015"]
    assert [paper.title for paper in by_year_only] == ["SRAM 2014"]


def test_soft_deleted_papers_are_not_returned(db_session) -> None:
    from datetime import datetime, timezone

    venue, edition = venues.resolve_venue(db_session, name="ISSCC", year=2015)
    paper = make_paper(db_session, deleted_at=datetime.now(timezone.utc))
    venues.attach_venue(db_session, paper, venue, edition)

    assert venues.find_papers_by_venue(db_session, name="ISSCC") == []


def test_venue_year_falls_back_to_the_paper_year(db_session) -> None:
    """A venue without parseable year still supports year filtering."""
    paper = make_paper(db_session, year=2015)
    venue = venues.get_or_create_venue(db_session, "ISSCC")

    venues.attach_venue(db_session, paper, venue, None)

    assert paper.venue_year == 2015
    assert paper.venue_edition_id is None


def test_count_venue_editions(db_session) -> None:
    venue, _ = venues.resolve_venue(db_session, name="ISSCC", year=2014)
    venues.resolve_venue(db_session, name="ISSCC", year=2015)

    assert venues.count_venue_editions(db_session, venue.id) == 2
    assert venues.venue_names_for_papers([]) == []


def test_find_venue_matches_normalized_names_only(db_session) -> None:
    venues.get_or_create_venue(db_session, "IEEE JSSC")

    assert venues.find_venue(db_session, "ieee jssc") is not None
    assert venues.find_venue(db_session, "ieee trans") is None
    assert venues.find_venue(db_session, "") is None