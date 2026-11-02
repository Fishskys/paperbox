import pytest

from app.parsing.metadata_fields import abstract_problem, author_problem, document_state, parse_authors
from app.services.pdf_byline import names_from_byline


@pytest.mark.parametrize("value", ["IEEE Journal;2024;1;2;10.1234/example", "Instructions for Paper Preparation", "2024 International Conference on Circuits"])
def test_publication_information_is_not_an_abstract(value):
    assert abstract_problem(value)


@pytest.mark.parametrize("value", ["We propose a TDC; its precision improves.", "This paper extends 10.1234/example.", "本文提出一种时间数字转换器。", "A compact TDC."])
def test_short_nonenglish_and_doi_abstracts_are_not_rejected(value):
    assert abstract_problem(value) is None


@pytest.mark.parametrize("value,expected", [
    (["Mar&#x00ED;a Moure", "Alice Smith, Bob Jones"], ["María Moure", "Alice Smith", "Bob Jones"]),
    ("Smith, John; Doe, Jane", ["Smith, John", "Doe, Jane"]),
    (["Alice Smith and Bob Jones"], ["Alice Smith", "Bob Jones"]),
    ("A. Smith, B. Jones", ["A. Smith", "B. Jones"]),
    ("Tasripan; 王小明; Ludwig van Beethoven", ["Tasripan", "王小明", "Ludwig van Beethoven"]),
])
def test_author_separators_and_entities(value, expected):
    assert parse_authors(value) == expected


def test_byline_accepts_mononyms_nonenglish_and_preserves_order():
    assert names_from_byline("Tasripan1, 王小明2 and Alice Smith3, Student Member, IEEE") == ["Tasripan", "王小明", "Alice Smith"]
    assert author_problem("IEEE")
    assert author_problem("user123")
    assert author_problem("alice@example.org")


def test_unsafe_document_text_does_not_receive_normal_quality():
    assert document_state("") == "missing_text"
    assert document_state("\ufffd" * 20) == "damaged_text"
    assert document_state("Text", [{"bbox": [0, 0, 10, 10], "text": "Alice"}, {"bbox": [0, 0, 10, 10], "text": "Bob"}]) == "overlapping_text"


@pytest.mark.parametrize("subject,expected", [("A compact TDC.", "A compact TDC."), ("IEEE Journal;2024;1;2;10.1234/example", None), ("An unrelated abstract.", None)])
def test_subject_requires_validity_and_body_corroboration(db_session, monkeypatch, subject, expected):
    from app.db.models import Paper, PaperSource, new_uuid
    from app.parsing.pdf import EmbeddedMetadata
    from app.services import metadata_reextract as reextract, metadata_service

    paper = Paper(id=new_uuid(), title="Example", fingerprint=new_uuid(), status="PENDING")
    db_session.add(paper)
    db_session.flush()
    monkeypatch.setattr(reextract, "extract_embedded_metadata", lambda _: EmbeddedMetadata(subject=subject, raw={}))
    monkeypatch.setattr(metadata_service, "extract_metadata", lambda *a, **k: {"abstract": "A compact TDC.", "quality": {"text_state": "ok"}})
    reextract.reextract_paper_metadata(db_session, paper, pages=[], pdf_bytes=b"fixture")
    from app.services import provenance_service as prov
    claim = prov.current_claim(db_session, paper.id, "abstract")
    assert claim.value == "A compact TDC."
    source = db_session.get(PaperSource, claim.source_id)
    assert source.source_type == ("pdf_embedded" if expected else "pdf_heuristic")
