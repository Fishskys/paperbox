"""Parse-artifact cache: replay from MinIO instead of re-parsing (plan T7.1).

Storage is always a dict-backed fake and the docling converter a callable
counter, so nothing here touches MinIO, docling or the embedding server. The
pypdf path uses the repository's small PDF fixture.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core.config import settings
from app.parsing.docling_client import DoclingResult, DoclingUnavailable
from app.services import parser_service
from app.services.object_storage import ObjectNotFound, ObjectStorageError

PAPER_ID = "11111111-2222-3333-4444-555555555555"
PARSE_PREFIX = f"papers/{PAPER_ID}/extracted/parsed"
MARKDOWN_KEY = f"{PARSE_PREFIX}/docling/document.md"
JSON_KEY = f"{PARSE_PREFIX}/docling/document.json"
META_KEY = f"{PARSE_PREFIX}/parse-meta.json"
PYPdf_MARKDOWN_KEY = f"{PARSE_PREFIX}/pypdf/document.md"

DOCLING_VERSION = "docling-serve 1.35.0 / docling 2.130.0"

DOCLING_MARKDOWN = (
    "# The Effect of Chopping on Comparator Offsets\n"
    "\n"
    "Body paragraph one.\n"
    "\n"
    "<!-- page-break -->\n"
    "\n"
    "## Measurement Setup\n"
    "\n"
    "Body paragraph two.\n"
)

DOCLING_JSON: dict = {
    "schema_name": "DoclingDocument",
    "version": "1.10.0",
    "texts": [{"label": "section_header", "text": "Measurement Setup"}],
}

SMOKE_PDF = Path(__file__).resolve().parent / "fixtures" / "smoke_sample.pdf"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeStore:
    """A dict-backed ``object_storage`` stand-in (mirrors the two methods used)."""

    def __init__(self, *, fail_read: bool = False, fail_write: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.calls: list[str] = []
        self.fail_read = fail_read
        self.fail_write = fail_write

    def download_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        self.calls.append(f"get:{object_key}")
        if self.fail_read:
            raise ObjectStorageError("minio is unreachable")
        try:
            return self.objects[object_key]
        except KeyError:
            raise ObjectNotFound(f"paperbox/{object_key} not found") from None

    def upload_bytes(
        self,
        object_key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> object:
        if self.fail_write:
            raise ObjectStorageError("minio is unreachable")
        self.calls.append(f"put:{object_key}")
        self.objects[object_key] = data
        self.content_types[object_key] = content_type
        return object()

    # helpers -----------------------------------------------------------------
    def meta(self) -> dict:
        return json.loads(self.objects[META_KEY].decode("utf-8"))

    def seed(self, meta_key: str, meta: dict) -> None:
        self.objects[meta_key] = json.dumps(meta).encode("utf-8")


class CountingConverter:
    """Counts conversions so "the backend was not called again" is provable."""

    def __init__(self, result: DoclingResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, data: bytes, *, filename: str, page_range: str | None = None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def docling_result(**overrides) -> DoclingResult:
    values = {
        "markdown": DOCLING_MARKDOWN,
        "page_count": 2,
        "parser_version": DOCLING_VERSION,
        "processing_time": 32.78,
        "raw_json": DOCLING_JSON,
    }
    values.update(overrides)
    return DoclingResult(**values)


def parse_docling(store: FakeStore, converter: CountingConverter, **kwargs):
    kwargs.setdefault("version_probe", lambda: DOCLING_VERSION)
    return parser_service.parse_paper_file(
        PAPER_ID,
        b"not read by the fake converter",
        filename="a.pdf",
        backend="docling",
        converter=converter,
        store=store,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# writing the artifacts
# --------------------------------------------------------------------------- #
def test_a_clean_parse_writes_markdown_json_and_meta() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())

    bundle = parse_docling(store, converter)

    assert converter.calls == 1
    assert bundle.cache_hit is False
    assert set(store.objects) == {MARKDOWN_KEY, JSON_KEY, META_KEY}
    # Everything hangs off the paper prefix, so delete_prefix(paper_id) (the
    # paper-deletion path) removes the artifacts with the paper.
    assert all(key.startswith(f"papers/{PAPER_ID}/extracted/") for key in store.objects)
    assert store.objects[MARKDOWN_KEY].decode("utf-8") == bundle.markdown
    assert json.loads(store.objects[JSON_KEY].decode("utf-8")) == DOCLING_JSON
    assert store.content_types[MARKDOWN_KEY].startswith("text/markdown")

    meta = store.meta()
    assert meta["cache_version"] == parser_service.PARSE_CACHE_VERSION
    assert meta["backend"] == "docling"
    assert meta["parser_version"] == DOCLING_VERSION
    assert meta["page_count"] == bundle.page_count == 2
    assert meta["degraded_reason"] is None
    assert meta["filename"] == "a.pdf"
    assert meta["artifacts"] == {"markdown": MARKDOWN_KEY, "json": JSON_KEY}
    assert meta["timings"]["docling_s"] == pytest.approx(0.0, abs=1.0)


def test_pypdf_artifacts_carry_no_docling_json() -> None:
    store = FakeStore()
    bundle = parser_service.parse_paper_file(
        PAPER_ID,
        SMOKE_PDF.read_bytes(),
        filename="smoke.pdf",
        backend="pypdf",
        store=store,
    )
    assert bundle.backend == "pypdf"
    assert bundle.cache_hit is False
    assert set(store.objects) == {PYPdf_MARKDOWN_KEY, META_KEY}
    meta = store.meta()
    assert meta["backend"] == "pypdf"
    assert meta["artifacts"]["json"] is None
    assert meta["page_count"] == bundle.page_count


# --------------------------------------------------------------------------- #
# replaying
# --------------------------------------------------------------------------- #
def test_the_second_parse_replays_without_calling_the_backend() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())

    first = parse_docling(store, converter)
    second = parse_docling(store, converter)

    assert converter.calls == 1, "the cached parse must not be converted again"
    assert second.cache_hit is True
    assert second.markdown == first.markdown
    assert second.page_count == first.page_count
    assert second.spans == first.spans
    assert second.parser_version == first.parser_version
    assert second.raw_json == first.raw_json
    assert second.degraded_reason is None
    assert second.timings["cache_load_s"] >= 0.0
    # The original numbers are replayed too: a report can still say how long the
    # parse cost when it was actually made.
    assert "docling_s" in second.timings


def test_a_replayed_bundle_keeps_the_pages_and_headings() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    first = parse_docling(store, converter)

    # Re-write the meta with headings and page_range recorded, as a docling parse
    # with a page limit would.
    meta = store.meta()
    meta["headings"] = [[1, "The Effect of Chopping on Comparator Offsets"]]
    meta["page_range"] = "1-2"
    store.seed(META_KEY, meta)

    second = parse_docling(store, converter)

    assert converter.calls == 1
    assert second.headings == [(1, "The Effect of Chopping on Comparator Offsets")]
    assert second.page_count == first.page_count
    assert [span.page for span in second.spans] == [span.page for span in first.spans]


# --------------------------------------------------------------------------- #
# when the entry must NOT be replayed
# --------------------------------------------------------------------------- #
def test_a_degraded_parse_is_stored_but_never_replayed() -> None:
    """A docling outage must not become sticky: the artifacts are written so the
    degradation is inspectable, but the next ingest tries docling again."""
    store = FakeStore()
    converter = CountingConverter(error=DoclingUnavailable("http 502: empty body"))

    first = parser_service.parse_paper_file(
        PAPER_ID,
        SMOKE_PDF.read_bytes(),
        filename="smoke.pdf",
        backend="docling",
        converter=converter,
        store=store,
        version_probe=lambda: DOCLING_VERSION,
    )
    assert first.backend == "pypdf"
    assert first.degraded_reason is not None
    assert first.degraded_reason.startswith("docling unavailable")
    assert store.meta()["degraded_reason"] == first.degraded_reason
    assert store.meta()["backend"] == "pypdf"

    second = parser_service.parse_paper_file(
        PAPER_ID,
        SMOKE_PDF.read_bytes(),
        filename="smoke.pdf",
        backend="docling",
        converter=converter,
        store=store,
        version_probe=lambda: DOCLING_VERSION,
    )
    assert converter.calls == 2, "the backend gets another chance"
    assert second.cache_hit is False


def test_a_degraded_entry_written_for_the_same_backend_is_not_replayed() -> None:
    """Even a degraded *docling-labelled* entry (e.g. formula fallback) re-parses."""
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    meta = store.meta()
    meta["degraded_reason"] = "formulas=text"
    store.seed(META_KEY, meta)

    parse_docling(store, converter)
    assert converter.calls == 2


def test_the_other_backends_artifacts_are_not_served() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    bundle = parser_service.parse_paper_file(
        PAPER_ID,
        SMOKE_PDF.read_bytes(),
        filename="smoke.pdf",
        backend="pypdf",
        store=store,
    )

    assert bundle.backend == "pypdf"
    assert bundle.cache_hit is False
    assert PYPdf_MARKDOWN_KEY in store.objects
    assert MARKDOWN_KEY in store.objects, "the other backend's artifact is left alone"
    assert store.meta()["backend"] == "pypdf"


def test_a_cache_version_bump_invalidates_the_entry() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    meta = store.meta()
    meta["cache_version"] = parser_service.PARSE_CACHE_VERSION + 1
    store.seed(META_KEY, meta)

    bundle = parse_docling(store, converter)
    assert converter.calls == 2
    assert bundle.cache_hit is False


def test_a_parser_version_change_invalidates_the_entry() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    bundle = parse_docling(
        store,
        converter,
        version_probe=lambda: "docling-serve 1.35.0 / docling 2.140.0",
    )
    assert converter.calls == 2
    assert bundle.cache_hit is False


def test_an_unreachable_version_probe_still_replays() -> None:
    """``None`` means "nobody answered", which is not evidence of an upgrade."""
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    bundle = parse_docling(store, converter, version_probe=lambda: None)
    assert converter.calls == 1
    assert bundle.cache_hit is True


def test_a_raising_version_probe_still_replays(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    def exploding() -> str | None:
        raise RuntimeError("probe exploded")

    with caplog.at_level(logging.WARNING):
        bundle = parse_docling(store, converter, version_probe=exploding)

    assert converter.calls == 1
    assert bundle.cache_hit is True
    assert "docling version probe failed" in caplog.text


def test_a_missing_markdown_artifact_turns_the_replay_into_a_parse() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)
    del store.objects[MARKDOWN_KEY]

    bundle = parse_docling(store, converter)
    assert converter.calls == 2
    assert bundle.cache_hit is False


# --------------------------------------------------------------------------- #
# the cache is an optimisation, never a dependency
# --------------------------------------------------------------------------- #
def test_a_broken_meta_artifact_falls_back_to_parsing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    store.objects[META_KEY] = b"{not json"

    with caplog.at_level(logging.WARNING):
        bundle = parse_docling(store, converter)

    assert converter.calls == 1
    assert bundle.cache_hit is False
    assert "parse cache meta is unreadable" in caplog.text
    assert json.loads(store.objects[META_KEY].decode("utf-8"))["backend"] == "docling"


def test_a_meta_artifact_pointing_outside_the_paper_is_ignored() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())
    parse_docling(store, converter)

    meta = store.meta()
    meta["artifacts"] = {
        "markdown": "papers/someone-else/extracted/parsed/docling/document.md"
    }
    store.objects["papers/someone-else/extracted/parsed/docling/document.md"] = b"# stolen\n"
    store.seed(META_KEY, meta)

    parse_docling(store, converter)
    assert converter.calls == 2


def test_a_storage_read_failure_still_parses(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeStore(fail_read=True)
    converter = CountingConverter(docling_result())

    with caplog.at_level(logging.WARNING):
        bundle = parse_docling(store, converter)

    assert converter.calls == 1
    assert bundle.cache_hit is False
    assert bundle.markdown
    assert "parse cache unreadable, parsing again" in caplog.text


def test_a_storage_write_failure_does_not_fail_the_parse(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(fail_write=True)
    converter = CountingConverter(docling_result())

    with caplog.at_level(logging.WARNING):
        bundle = parse_docling(store, converter)

    assert bundle.markdown
    assert bundle.cache_hit is False
    assert store.objects == {}
    assert "could not write the parse artifacts" in caplog.text


def test_cache_disabled_neither_reads_nor_writes() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())

    first = parse_docling(store, converter, cache=False)
    second = parse_docling(store, converter, cache=False)

    assert (first.cache_hit, second.cache_hit) == (False, False)
    assert converter.calls == 2
    assert store.calls == []


def test_the_setting_decides_when_the_argument_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PARSER_CACHE=false`` must turn the whole layer off, not just the read."""
    store = FakeStore()
    converter = CountingConverter(docling_result())
    monkeypatch.setattr(parser_service.settings, "parser_cache", False)

    parse_docling(store, converter)
    parse_docling(store, converter)

    assert converter.calls == 2
    assert store.objects == {}


# --------------------------------------------------------------------------- #
# partial parses stay out of the cache (2026-09-29 T8)
# --------------------------------------------------------------------------- #
def test_a_partial_parse_is_neither_read_nor_written() -> None:
    store = FakeStore()
    converter = CountingConverter(docling_result())

    bundle = parse_docling(store, converter, page_range="1-2")

    assert converter.calls == 1
    assert bundle.cache_hit is False
    # No artifact write at all: not even a meta file, so a later full parse
    # cannot mistake this slice for the document.
    assert store.objects == {}
    assert store.calls == []


def test_parser_max_pages_also_disables_the_cache(monkeypatch) -> None:
    monkeypatch.setattr(settings, "parser_max_pages", 4)
    store = FakeStore()
    converter = CountingConverter(docling_result())

    bundle = parse_docling(store, converter)

    assert converter.calls == 1
    assert bundle.cache_hit is False
    assert store.objects == {}


def test_a_stored_full_parse_is_not_replayed_for_a_partial_request() -> None:
    store = FakeStore()
    first = CountingConverter(docling_result())
    parse_docling(store, first)
    assert first.calls == 1
    assert set(store.objects) == {MARKDOWN_KEY, JSON_KEY, META_KEY}

    second = CountingConverter(docling_result())
    parse_docling(store, second, page_range="1-1")

    # The slice must be parsed, not answered from the full-document artifact.
    assert second.calls == 1


def test_a_partial_parse_does_not_evict_the_full_one() -> None:
    store = FakeStore()
    parse_docling(store, CountingConverter(docling_result()), page_range="1-1")
    written_by_slice = dict(store.objects)

    converter = CountingConverter(docling_result())
    bundle = parse_docling(store, converter)

    assert converter.calls == 1
    assert bundle.cache_hit is False  # nothing was cached by the slice
    assert set(store.objects) == set(written_by_slice) | {MARKDOWN_KEY, JSON_KEY, META_KEY}
