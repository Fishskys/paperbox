"""Degradation ledger: every stage that gave up on something says so (plan T7.3).

A degraded result is usable -- docling down means pypdf, embedding down means
length chunking -- so nothing here fails an ingest. What these tests pin is the
*visibility*: one row per ``(paper, stage, code)``, repeated reports counted
instead of duplicated, and a stage that later runs clean resolving its own rows
while another stage's stay open.
"""

from __future__ import annotations

import io
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.api.papers import router as papers_router  # noqa: F401 - import side effect
from app.core.security import require_api_key
from app.db.models import Paper, PaperDegradation, new_uuid
from datetime import datetime, timezone

from app.db.session import get_db
from app.main import app
from app.parsing.chunking import chunk_document
from app.parsing.docling_client import FORMULA_FALLBACK_REASON, DoclingUnavailable
from app.parsing.pdf import PageText
from app.parsing.structure import Section
from app.services import degradation_service as ledger
from app.services import parser_service
from tests.conftest import build_session_factory

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def add_paper(session, **overrides) -> str:
    paper = Paper(
        id=new_uuid(),
        title="A Paper",
        fingerprint=f"sha256:{new_uuid()}",
        status="INDEXED",
        **overrides,
    )
    session.add(paper)
    session.flush()
    return paper.id


class Sink:
    """A ``DegradeSink`` that keeps everything (the chunker/parser only calls it)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, stage: str, code: str, detail: dict | None = None) -> None:
        self.calls.append((stage, code, dict(detail or {})))

    @property
    def codes(self) -> list[str]:
        return [code for _, code, _ in self.calls]


def page(text: str, number: int = 1) -> PageText:
    return PageText(page=number, text=text)


def section(text: str, *, sentences: int = 12) -> Section:
    """One section carrying ``text`` split into sentences as paragraphs."""
    pieces = [text[index : index + 120] for index in range(0, len(text), 120)] or [text]
    return Section(
        title="1 Introduction",
        page_start=1,
        page_end=1,
        paragraphs=[(1, piece) for piece in pieces],
    )


def pdf_bytes(text: str = "The comparator offsets are cancelled.") -> bytes:
    writer = PdfWriter()
    pdf_page = writer.add_blank_page(width=612, height=792)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    pdf_page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    pdf_page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                        }
                    )
                }
            )
        }
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# record / resolve: one row per (paper, stage, code)
# --------------------------------------------------------------------------- #


def test_record_creates_one_open_row(db_session) -> None:
    paper_id = add_paper(db_session)
    row = ledger.record(
        db_session,
        paper_id=paper_id,
        stage=ledger.STAGE_CHUNKING,
        code="semantic_fallback",
        detail={"section": "1 Introduction"},
    )
    assert row.stage == "chunking"
    assert row.occurrences == 1
    assert row.resolved_at is None
    assert row.first_seen_at is not None
    assert row.last_seen_at is not None
    assert row.detail == {"section": "1 Introduction"}


def test_repeated_report_counts_instead_of_duplicating(db_session) -> None:
    paper_id = add_paper(db_session)
    for sentence_count in (3, 4, 5):
        ledger.record(
            db_session,
            paper_id=paper_id,
            stage=ledger.STAGE_CHUNKING,
            code="semantic_fallback",
            detail={"sentences": sentence_count},
        )
    rows = ledger.list_for_paper(db_session, paper_id)
    assert len(rows) == 1
    assert rows[0].occurrences == 3
    # the detail is the latest truth, not the first
    assert rows[0].detail == {"sentences": 5}
    assert db_session.query(PaperDegradation).count() == 1


def test_different_stages_and_codes_coexist(db_session) -> None:
    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_PARSING, code="docling_unavailable"
    )
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_PARSING, code="formulas_as_text"
    )
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    rows = ledger.list_for_paper(db_session, paper_id)
    assert [(row.stage, row.code) for row in rows] == [
        ("chunking", "semantic_fallback"),
        ("parsing", "docling_unavailable"),
        ("parsing", "formulas_as_text"),
    ]
    assert ledger.stages_for_report(rows) == {
        "chunking": ["semantic_fallback"],
        "parsing": ["docling_unavailable", "formulas_as_text"],
    }


def test_unknown_stage_is_rejected_so_a_typo_cannot_invent_one(db_session) -> None:
    paper_id = add_paper(db_session)
    with pytest.raises(ValueError, match="unknown degradation stage"):
        ledger.record(
            db_session, paper_id=paper_id, stage="chunkng", code="semantic_fallback"
        )


def test_empty_code_is_rejected(db_session) -> None:
    paper_id = add_paper(db_session)
    with pytest.raises(ValueError, match="must not be empty"):
        ledger.record(
            db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="  "
        )


def test_resolve_closes_only_what_the_run_did_not_report(db_session) -> None:
    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_PARSING, code="docling_unavailable"
    )

    resolved = ledger.resolve_stage(
        db_session,
        paper_id=paper_id,
        stage=ledger.STAGE_CHUNKING,
        keep=["length_mode_tuning"],
    )
    assert resolved == 1
    # the chunking row is closed, the parsing row is untouched
    assert [row.code for row in ledger.list_for_paper(db_session, paper_id)] == [
        "docling_unavailable"
    ]
    still_there = ledger.list_for_paper(db_session, paper_id, include_resolved=True)
    assert {row.code for row in still_there} == {"semantic_fallback", "docling_unavailable"}


def test_a_kept_code_stays_open(db_session) -> None:
    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    assert (
        ledger.resolve_stage(
            db_session,
            paper_id=paper_id,
            stage=ledger.STAGE_CHUNKING,
            keep=["semantic_fallback"],
        )
        == 0
    )
    assert ledger.paper_ids_with_open_degradations(db_session) == {paper_id}


def test_a_recurrence_reopens_a_resolved_row(db_session) -> None:
    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    ledger.resolve_stage(db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING)
    assert ledger.paper_ids_with_open_degradations(db_session) == set()

    again = ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    assert again.resolved_at is None
    assert again.occurrences == 2


def test_open_lookups_can_be_narrowed_by_stage_and_code(db_session) -> None:
    first = add_paper(db_session)
    second = add_paper(db_session)
    ledger.record(
        db_session, paper_id=first, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    ledger.record(
        db_session, paper_id=first, stage=ledger.STAGE_PARSING, code="docling_unavailable"
    )
    ledger.record(
        db_session, paper_id=second, stage=ledger.STAGE_PARSING, code="docling_unavailable"
    )

    assert ledger.paper_ids_with_open_degradations(db_session) == {first, second}
    assert ledger.paper_ids_with_open_degradations(
        db_session, stage=ledger.STAGE_CHUNKING
    ) == {first}
    assert ledger.paper_ids_with_open_degradations(
        db_session, code="docling_unavailable"
    ) == {first, second}
    assert [
        (row.paper_id, row.stage) for row in
        ledger.open_degradations(db_session, paper_id=second)
    ] == [(second, "parsing")]


def test_job_id_is_kept_for_triage(db_session) -> None:
    paper_id = add_paper(db_session)
    row = ledger.record(
        db_session,
        paper_id=paper_id,
        stage=ledger.STAGE_CHUNKING,
        code="semantic_fallback",
        job_id=new_uuid(),
    )
    assert row.job_id is not None


# --------------------------------------------------------------------------- #
# Recorder: the sink the pipeline hands to the stages
# --------------------------------------------------------------------------- #


def test_recorder_records_and_resolves_per_stage(db_session) -> None:
    paper_id = add_paper(db_session)
    recorder = ledger.Recorder(db_session, paper_id=paper_id, job_id=new_uuid())
    recorder("chunking", "semantic_fallback", {"section": "2 Method"})
    assert recorder.seen("chunking") == frozenset({"semantic_fallback"})
    assert recorder.seen("parsing") == frozenset()

    # a later clean run of the same stage closes what it does not report
    clean_run = ledger.Recorder(db_session, paper_id=paper_id)
    assert clean_run.resolve("chunking") == 1
    assert ledger.paper_ids_with_open_degradations(db_session) == set()


def test_recorder_never_fails_the_run_it_describes(db_session, caplog) -> None:
    paper_id = add_paper(db_session)
    recorder = ledger.Recorder(db_session, paper_id=paper_id)
    with caplog.at_level("ERROR"):
        recorder("chunkng", "semantic_fallback")  # typo: logged, not raised
    assert recorder.seen("chunking") == frozenset()
    assert db_session.query(PaperDegradation).count() == 0
    assert "could not record degradation" in caplog.text


# --------------------------------------------------------------------------- #
# chunking: the semantic fallback reports through the sink
# --------------------------------------------------------------------------- #


def failing_embedder(texts):
    raise RuntimeError("embedding server refused")


def paragraphs(count: int = 12) -> str:
    return " ".join(
        f"This is sentence number {index} of the section under test."
        for index in range(count)
    )


def test_semantic_fallback_reports_section_and_error() -> None:
    text = paragraphs()
    sink = Sink()
    chunks = chunk_document(
        [page(text)],
        [section(text)],
        embed_fn=failing_embedder,
        on_degrade=sink,
        semantic_min_tokens=1,
    )
    assert chunks, "the fallback must still produce chunks"
    assert sink.calls, "a fallback must be reported"
    stage, code, detail = sink.calls[0]
    assert (stage, code) == ("chunking", "semantic_fallback")
    assert detail["section"] == "1 Introduction"
    assert "RuntimeError" in detail["error"]


def test_length_mode_reports_nothing() -> None:
    text = paragraphs()
    sink = Sink()
    chunk_document([page(text)], [section(text)], on_degrade=sink)
    assert sink.calls == []


def test_a_fallback_without_a_sink_still_chunks() -> None:
    text = paragraphs()
    chunks = chunk_document(
        [page(text)], [section(text)], embed_fn=failing_embedder, semantic_min_tokens=1
    )
    assert chunks


def test_a_working_embedder_reports_nothing() -> None:
    text = paragraphs()
    sink = Sink()

    def embedder(texts):
        return [[1.0, 0.0] for _ in texts]

    chunk_document(
        [page(text)],
        [section(text)],
        embed_fn=embedder,
        on_degrade=sink,
        semantic_min_tokens=1,
    )
    assert sink.calls == []


# --------------------------------------------------------------------------- #
# parsing: one row per cause in ``degraded_reason``
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("docling unavailable DoclingUnavailable: http 502", ["docling_unavailable"]),
        (FORMULA_FALLBACK_REASON, ["formulas_as_text"]),
        ("no formula latex", ["formulas_as_text"]),
        ("reading order not verified", ["reading_order_unverified"]),
        ("something new nobody mapped", ["parse_degraded"]),
    ],
)
def test_degradation_codes_cover_every_known_reason(reason: str, expected: list[str]) -> None:
    assert parser_service.degradation_codes(reason) == expected


def test_a_reason_that_mentions_two_causes_produces_two_codes() -> None:
    reason = f"docling unavailable DoclingUnavailable: http 502; {FORMULA_FALLBACK_REASON}"
    assert parser_service.degradation_codes(reason) == [
        "formulas_as_text",
        "docling_unavailable",
    ]


def test_docling_failure_reports_through_the_sink(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise DoclingUnavailable("http 502: empty body")

    monkeypatch.setattr(parser_service, "_parse_with_docling", boom)
    sink = Sink()
    bundle = parser_service.parse_pdf(
        pdf_bytes(), filename="a.pdf", backend="docling", on_degrade=sink
    )
    assert bundle.backend == "pypdf"
    assert bundle.degraded_reason
    # A docling outage costs both: docling itself, and the formulas pypdf can
    # only render as text.
    assert sorted(sink.codes) == ["docling_unavailable", "formulas_as_text"]
    assert {call[2]["backend"] for call in sink.calls} == {"pypdf"}
    assert all(call[2]["reason"] == bundle.degraded_reason for call in sink.calls)


def test_a_clean_docling_parse_reports_nothing() -> None:
    from app.parsing.docling_client import DoclingResult

    def convert(data, *, filename, page_range=None):
        return DoclingResult(
            markdown="# Title\n\nBody text.\n",
            page_count=1,
            parser_version="fake 1.0",
        )

    sink = Sink()
    bundle = parser_service.parse_pdf(
        b"ignored", filename="a.pdf", backend="docling", converter=convert, on_degrade=sink
    )
    assert bundle.degraded_reason is None
    assert sink.calls == []


def test_a_pypdf_parse_reports_its_own_layout_degradation() -> None:
    sink = Sink()
    bundle = parser_service.parse_pdf(
        pdf_bytes(), filename="a.pdf", backend="pypdf", on_degrade=sink
    )
    if bundle.degraded_reason is None:
        assert sink.calls == []
    else:
        assert sink.calls, "a degraded pypdf parse must be reported"


class _InMemoryStore:
    """Never let a unit test reach the real MinIO bucket (2026-09-29).

    T7.3 first wrote this test without a store: ``parse_paper_file`` then fell
    back to ``_default_store()`` and cached the artifact in the *live* bucket
    under a random uuid, which showed up as two extra orphan objects in
    ``GET /api/consistency``. Unit tests must not touch real infrastructure.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_bytes(self, key: str, data: bytes, **kwargs) -> None:
        self.objects[key] = data

    def download_bytes(self, key: str) -> bytes:
        from app.services.object_storage import ObjectNotFound

        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]


def test_parse_paper_file_passes_the_sink_through(monkeypatch, tmp_path) -> None:
    """The pipeline's entry point must not swallow the sink."""
    seen: list[tuple] = []

    def fake_parse_pdf(data, **kwargs):
        seen.append(kwargs.get("on_degrade"))
        from app.parsing.markdown import ParseBundle

        return ParseBundle(
            markdown="Body.\n",
            backend="pypdf",
            parser_version="x",
            page_count=1,
            spans=[],
            degraded_reason=None,
            timings={},
        )

    monkeypatch.setattr(parser_service, "parse_pdf", fake_parse_pdf)
    sink = Sink()
    store = _InMemoryStore()
    parser_service.parse_paper_file(
        new_uuid(),
        pdf_bytes(),
        filename="a.pdf",
        backend="pypdf",
        on_degrade=sink,
        store=store,
    )
    assert seen == [sink]
    # The parse went through: the sink was honoured *and* nothing left the process.
    assert store.objects


# --------------------------------------------------------------------------- #
# API: GET /api/papers/{id}/degradations
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client():
    factory, engine = build_session_factory()

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_degradations_endpoint_lists_open_rows(client) -> None:
    http, factory = client
    session = factory()
    paper_id = add_paper(session)
    ledger.record(
        session,
        paper_id=paper_id,
        stage=ledger.STAGE_CHUNKING,
        code="semantic_fallback",
        detail={"section": "2 Method"},
    )
    session.commit()
    session.close()

    body = http.get(f"/api/papers/{paper_id}/degradations").json()
    assert body["paper_id"] == paper_id
    assert body["total"] == 1
    assert body["degraded"] is True
    assert body["degradations"][0]["stage"] == "chunking"
    assert body["degradations"][0]["code"] == "semantic_fallback"
    assert body["degradations"][0]["detail"] == {"section": "2 Method"}
    assert body["degradations"][0]["resolved_at"] is None


def test_degradations_endpoint_hides_resolved_rows_unless_asked(client) -> None:
    http, factory = client
    session = factory()
    paper_id = add_paper(session)
    ledger.record(
        session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    ledger.resolve_stage(session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING)
    session.commit()
    session.close()

    open_only = http.get(f"/api/papers/{paper_id}/degradations").json()
    assert open_only == {
        "paper_id": paper_id,
        "total": 0,
        "degraded": False,
        "degradations": [],
    }
    everything = http.get(
        f"/api/papers/{paper_id}/degradations", params={"include_resolved": True}
    ).json()
    assert everything["total"] == 1
    assert everything["degraded"] is False
    assert everything["degradations"][0]["resolved_at"] is not None


def test_degradations_endpoint_is_empty_for_a_clean_paper(client) -> None:
    http, factory = client
    session = factory()
    paper_id = add_paper(session)
    session.commit()
    session.close()
    body = http.get(f"/api/papers/{paper_id}/degradations").json()
    assert body["total"] == 0 and body["degraded"] is False


def test_degradations_endpoint_404s_on_an_unknown_paper(client) -> None:
    http, _factory = client
    response = http.get(f"/api/papers/{new_uuid()}/degradations")
    assert response.status_code == 404


def test_deleting_a_paper_takes_its_degradations_with_it(db_session) -> None:
    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    db_session.delete(db_session.get(Paper, paper_id))
    db_session.flush()
    assert db_session.query(PaperDegradation).count() == 0


# --------------------------------------------------------------------------- #
# script: reindex --degraded
# --------------------------------------------------------------------------- #


def test_reindex_targets_can_select_degraded_papers(db_session) -> None:
    from scripts.reindex import targets

    clean = add_paper(db_session)
    degraded = add_paper(db_session)
    ledger.record(
        db_session, paper_id=degraded, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )

    # targets() 现在返回 (papers, reasons)：理由要能报给调用方（端点与 --auto 都要用）
    assert {paper.id for paper in targets(db_session, [], False)[0]} == {clean, degraded}
    assert {paper.id for paper in targets(db_session, [], False, True)[0]} == {degraded}
    assert {paper.id for paper in targets(db_session, [clean], False, True)[0]} == set()


def test_reindex_targets_skip_papers_whose_degradation_is_resolved(db_session) -> None:
    from scripts.reindex import targets

    paper_id = add_paper(db_session)
    ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    ledger.resolve_stage(db_session, paper_id=paper_id, stage=ledger.STAGE_CHUNKING)
    assert targets(db_session, [], False, True)[0] == []


def test_reindex_degradations_listing_shows_stage_code_pairs(
    db_session, capsys, monkeypatch
) -> None:
    from scripts import reindex

    paper_id = add_paper(db_session)
    ledger.record(
        db_session,
        paper_id=paper_id,
        stage=ledger.STAGE_PARSING,
        code="docling_unavailable",
        detail={"reason": "http 502"},
    )
    db_session.commit()

    class Factory:
        def __call__(self):
            return db_session

    monkeypatch.setattr(reindex, "SessionLocal", Factory())
    monkeypatch.setattr(reindex.opensearch, "ensure_index", lambda *a, **k: None)
    monkeypatch.setattr(reindex.settings, "embedding_model", "test-model")
    monkeypatch.setattr(reindex.settings, "opensearch_alias", "test-alias")

    import sys

    monkeypatch.setattr(
        sys, "argv", ["reindex.py", "--degradations", "--degraded-stage", "parsing"]
    )
    assert reindex.main() == 0
    out = capsys.readouterr().out
    assert "1 open degradation(s)" in out
    assert f"{paper_id}: parsing/docling_unavailable" in out


def test_reindex_targets_can_select_a_backend_stamp(db_session) -> None:
    """``--parser-backend`` reads the stamp, so it also catches a configured backend."""
    from scripts.reindex import UNKNOWN_BACKEND, targets

    docling = add_paper(db_session, parser_backend="docling", parser_version="2.1")
    pypdf = add_paper(db_session, parser_backend="pypdf", parser_version="6.18")
    legacy = add_paper(db_session)  # no stamp at all
    gone = add_paper(db_session, parser_backend="pypdf")
    gone_paper = db_session.get(Paper, gone)
    gone_paper.deleted_at = datetime.now(timezone.utc)
    db_session.flush()

    def picked(**kwargs) -> set[str]:
        return {paper.id for paper in targets(db_session, [], False, **kwargs)[0]}

    assert picked() == {docling, pypdf, legacy}
    assert picked(parser_backend="docling") == {docling}
    assert picked(parser_backend="pypdf") == {pypdf}
    assert picked(parser_backend=UNKNOWN_BACKEND) == {legacy}


def test_reindex_targets_combine_the_stamp_with_the_ledger(db_session) -> None:
    """Both filters are AND-ed: "fell back to pypdf", not "is stamped pypdf"."""
    from scripts.reindex import targets

    fell_back = add_paper(db_session, parser_backend="pypdf")
    by_choice = add_paper(db_session, parser_backend="pypdf")
    ledger.record(
        db_session,
        paper_id=fell_back,
        stage=ledger.STAGE_PARSING,
        code="docling_unavailable",
    )
    chosen = {
        paper.id
        for paper in targets(
            db_session,
            [],
            False,
            True,
            degraded_stage=ledger.STAGE_PARSING,
            degraded_code="docling_unavailable",
            parser_backend="pypdf",
        )[0]
    }
    assert chosen == {fell_back}
    assert by_choice not in chosen


def test_reindex_dry_run_lists_the_selection_without_indexing(
    db_session, capsys, monkeypatch
) -> None:
    from scripts import reindex

    pypdf = add_paper(db_session, parser_backend="pypdf", parser_version="6.18")
    add_paper(db_session, parser_backend="docling", parser_version="2.1")
    db_session.commit()

    class Factory:
        def __call__(self):
            return db_session

    touched: list[str] = []
    monkeypatch.setattr(reindex, "SessionLocal", Factory())
    monkeypatch.setattr(
        reindex.opensearch, "ensure_index", lambda *a, **k: touched.append("index")
    )
    monkeypatch.setattr(
        reindex.tasks, "reindex_paper", lambda *a, **k: touched.append("reindex")
    )
    monkeypatch.setattr(reindex.settings, "embedding_model", "test-model")
    monkeypatch.setattr(reindex.settings, "opensearch_alias", "test-alias")

    import sys

    monkeypatch.setattr(
        sys, "argv", ["reindex.py", "--parser-backend", "pypdf", "--dry-run"]
    )
    assert reindex.main() == 0
    out = capsys.readouterr().out
    assert "would reindex 1 paper(s)" in out
    assert f"{pypdf}: stamp=pypdf degraded=-" in out
    assert touched == []


def test_job_id_foreign_key_column_is_optional(db_session) -> None:
    """A script-side degradation has no job, and that must be fine."""
    paper_id = add_paper(db_session)
    row = ledger.record(
        db_session, paper_id=paper_id, stage=ledger.STAGE_INDEXING, code="snapshot_lag"
    )
    assert row.job_id is None


def test_reindex_auto_reports_the_detected_reasons(db_session) -> None:
    """``--auto`` 与 API 端点共用同一套检测：理由要随选择一起返回，才能解释"为什么是这些"。"""
    from scripts.reindex import targets

    degraded = add_paper(db_session)
    ledger.record(
        db_session, paper_id=degraded, stage=ledger.STAGE_CHUNKING, code="semantic_fallback"
    )
    clean = add_paper(db_session)
    db_session.flush()

    papers, reasons = targets(db_session, [], False, auto=True)

    assert {paper.id for paper in papers} >= {degraded}
    codes = {reason.code for reason in reasons}
    # clean 没有 chunk，所以 missing_chunks 也会命中；降级那条必须在
    assert "open_degradations" in codes
    assert all(reason.papers >= 1 for reason in reasons)
