"""Independent bibliographic parsers validate exported records."""

from datetime import date

import bibtexparser
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylatexenc.latex2text import LatexNodes2Text
import rispy

from app.api.papers import router
from app.db.session import get_db
from app.main import AuthContextMiddleware
from app.services import citation_service, paper_service, venue_service
from tests.test_auth_rest import key_material  # noqa: F401
from tests.test_metadata_merge import make_paper


def test_bibtex_round_trip_keeps_authors_doi_and_special_characters(db_session):
    title = r'A {CMOS} & 50% Circuit #2 with $5_cost and \path'
    paper = make_paper(db_session, title=title, doi="10.1234/a_b", year=2020, paper_type="journal", volume="2", issue="3", pages="10–20")
    paper.venue, _ = venue_service.resolve_venue(db_session, name="Circuits & Systems")
    paper_service.set_paper_authors(db_session, paper, ["Smith, John", "Giuseppe Avolio"])
    db_session.commit()
    content, media, extension = citation_service.export(db_session, paper.id, "bibtex")
    library = bibtexparser.parse_string(content)
    assert not library.failed_blocks and len(library.entries) == 1
    entry = library.entries[0]
    assert entry.entry_type == "article"
    values = {key: field.value for key, field in entry.fields_dict.items()}
    assert LatexNodes2Text().latex_to_text(values["title"]) == title
    assert values["author"] == "Smith, John and Giuseppe Avolio"
    assert values["doi"] == "10.1234/a_b"
    assert values["pages"] == "10--20"
    assert (media, extension) == ("application/x-bibtex", "bib")


def test_ris_round_trip_and_record_injection_protection(db_session):
    paper = make_paper(db_session, title="A Circuit\nER  -\nTY  - JOUR", doi="10.1234/a_b", year=2020,
        paper_type="conference", pages="10-20", publication_date=date(2020, 5, 4))
    paper_service.set_paper_authors(db_session, paper, ["Giuseppe Avolio", "Smith, John"])
    db_session.commit()
    content, _, extension = citation_service.export(db_session, paper.id, "ris")
    records = rispy.loads(content)
    assert len(records) == 1
    record = records[0]
    assert record["type_of_reference"] == "CONF"
    assert record["authors"] == ["Giuseppe Avolio", "Smith, John"]
    assert record["doi"] == "10.1234/a_b"
    assert record["start_page"] == "10" and record["end_page"] == "20"
    assert record["date"] == "2020-05-04"
    assert record["title"] == "A Circuit ER - TY - JOUR" and extension == "ris"


def test_missing_fields_are_omitted_and_bibtex_entry_injection_is_escaped(db_session):
    paper = make_paper(db_session, title='A }\n@article{injected, title={Danger}} Circuit')
    db_session.commit()
    content, _, _ = citation_service.export(db_session, paper.id, "bibtex")
    parsed = bibtexparser.parse_string(content)
    assert len(parsed.entries) == 1 and not parsed.failed_blocks
    assert parsed.entries[0].entry_type == "misc"
    assert not {"author", "year", "doi", "journal"} & parsed.entries[0].fields_dict.keys()
    ris = rispy.loads(citation_service.export(db_session, paper.id, "ris")[0])[0]
    assert not {"authors", "year", "doi"} & ris.keys()
    text = citation_service.export(db_session, paper.id, "text")[0]
    assert "None" not in text and "未知" not in text


def test_read_key_exports_current_metadata_without_writes(session_factory, key_material):
    isolated = FastAPI()
    isolated.include_router(router)
    isolated.add_middleware(AuthContextMiddleware)
    def db():
        with session_factory() as session: yield session
    isolated.dependency_overrides[get_db] = db
    with session_factory() as session:
        paper = make_paper(session, title="A Current Title", doi="10.1234/example")
        session.commit()
        paper_id = paper.id
    client = TestClient(isolated)
    headers = {"Authorization": f"Bearer {key_material['read']}"}
    for format in ("text", "bibtex", "ris"):
        result = client.get(f"/api/papers/{paper_id}/citation?format={format}", headers=headers)
        assert result.status_code == 200
        assert "A Current Title" in result.text
        assert "attachment" in result.headers["content-disposition"]
    assert client.get(f"/api/papers/{paper_id}/citation?format=invalid", headers=headers).status_code == 422
    assert client.get("/api/papers/invalid/citation", headers=headers).status_code == 404
    with session_factory() as session:
        paper = paper_service.get_paper(session, paper_id)
        paper.title = "An Updated Title"
        session.commit()
    assert "An Updated Title" in client.get(f"/api/papers/{paper_id}/citation", headers=headers).text