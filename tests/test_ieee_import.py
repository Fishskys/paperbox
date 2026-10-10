"""IEEE import: the field-by-field mapping and the import report.

The sample mirrors a real IEEE Xplore batch response (``{"total_records": …,
"articles": [...]}``) so every mapping of section 12 of the design is exercised:
identifiers, venue + edition, index-term kinds, month-precision date, and the
fields that are deliberately *not* stored structurally (author affiliations,
citation counts, license).
"""

from __future__ import annotations

from app.db.models import Paper, PaperSource, Venue, VenueEdition
from app.services import metadata_identifiers as ids
from app.services import metadata_import as importer
from app.services import metadata_merge as merge
from app.services import metadata_sources as sources
from app.services import metadata_tags as tags
from app.services import paper_service, provenance_service

DOI = "10.1109/JSSC.2015.2441234"

IEEE_SAMPLE = {
    "total_records": 1,
    "articles": [
        {
            "title": "A 0.6 V Low Power SRAM with Leakage Reduction",
            "abstract": "This paper presents a leakage reduction technique for SRAM.",
            "doi": DOI,
            "article_number": "7065247",
            "issn": "0018-9219",
            "publication_title": "IEEE Journal of Solid-State Circuits",
            "publication_year": 2015,
            "publication_date": "July 2015",
            "content_type": "Journals",
            "volume": "62",
            "issue": "7",
            "start_page": "631",
            "end_page": "635",
            "html_url": "https://ieeexplore.ieee.org/document/7065247",
            "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=7065247",
            "abstract_url": "https://ieeexplore.ieee.org/abstract/document/7065247",
            "authors": [
                {
                    "full_name": "Alice Smith",
                    "author_order": 1,
                    "affiliation": "Example University",
                    "id": "37085000000",
                },
                {
                    "full_name": "Bob Jones",
                    "author_order": 2,
                    "affiliation": "Example Corp",
                },
            ],
            "authorAffiliations": [{"author": "Alice Smith", "affiliation": "Example University"}],
            "index_terms": {
                "ieee_terms": {"terms": ["SRAM", "low-power electronics"]},
                "author_terms": {"terms": ["leakage reduction"]},
                "dynamic_index_terms": {"terms": ["subthreshold operation"]},
            },
            "publication_number": "12345",
            "is_number": "5678",
            "conference_location": "San Francisco, CA, USA",
            "conference_dates": "9-13 Feb. 2015",
            "citing_paper_count": 12,
            "download_count": 99,
            "insert_date": "2015-07-01T00:00:00Z",
            "license": "IEEE",
            "rank": 1,
        }
    ],
}


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": paper_service.new_uuid(),
        "title": "Placeholder",
        "fingerprint": f"sha256:{paper_service.new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def paper_with_doi(session, **overrides) -> Paper:
    paper = make_paper(session, **overrides)
    ids.upsert_identifier(session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value=DOI)
    ids.refresh_primary(session, paper.id)
    return paper


def seed_heuristic_title(session, paper, title) -> None:
    source = sources.upsert_source(
        session,
        source_type=sources.SOURCE_TYPE_PDF_HEURISTIC,
        source_ref=sources.paper_heuristic_ref(paper.id),
        raw={},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
    )
    provenance_service.set_field(
        session, paper, "title", title, source_id=source.id, override=True
    )


# --------------------------------------------------------------------------- #
# format detection and parsing
# --------------------------------------------------------------------------- #
def test_format_is_detected_from_the_payload() -> None:
    assert importer.detect_format(IEEE_SAMPLE) == importer.FORMAT_IEEE_RAW
    assert importer.detect_format([{"DOI": "10.1/x", "type": "article-journal"}]) == (
        importer.FORMAT_CSL_JSON
    )
    assert importer.detect_format([{"title": "T", "year": 2015}]) == importer.FORMAT_GENERIC
    assert importer.detect_format({"title": "T"}) == importer.FORMAT_GENERIC


def test_a_record_covers_every_mapped_field() -> None:
    parsed = importer.parse_records(IEEE_SAMPLE)[0]

    assert parsed.format == importer.FORMAT_IEEE_RAW
    assert parsed.title == "A 0.6 V Low Power SRAM with Leakage Reduction"
    assert parsed.authors == ["Alice Smith", "Bob Jones"]
    assert parsed.year == 2015
    assert parsed.content_type == "Journals"

    values = parsed.values
    assert values["volume"] == "62"
    assert values["issue"] == "7"
    assert values["pages"] == "631-635"
    assert values["publication_date"] == "2015-07-01"
    assert values["paper_type"] == "journal"
    assert values["identifier:doi"] == DOI
    assert values["identifier:ieee_article_number"] == "7065247"
    assert values["identifier:issn"] == "0018-9219"
    assert values["url"] == "https://ieeexplore.ieee.org/document/7065247"


def test_the_venue_claim_carries_the_edition_detail() -> None:
    venue = importer.parse_records(IEEE_SAMPLE)[0].values["venue"]

    assert venue["name"] == "IEEE Journal of Solid-State Circuits"
    assert venue["year"] == 2015
    assert venue["content_type"] == "Journals"
    assert venue["issn"] == "0018-9219"
    assert venue["publication_number"] == "12345"
    assert venue["is_number"] == "5678"
    assert venue["location"] == "San Francisco, CA, USA"
    assert venue["dates"] == "9-13 Feb. 2015"


def test_index_terms_keep_their_kind() -> None:
    values = importer.parse_records(IEEE_SAMPLE)[0].values

    assert values["tag:ieee_terms"] == ["SRAM", "low-power electronics"]
    assert values["tag:author_terms"] == ["leakage reduction"]
    assert values["tag:dynamic_index_terms"] == ["subthreshold operation"]


def test_author_affiliations_are_not_stored_structurally() -> None:
    parsed = importer.parse_records(IEEE_SAMPLE)[0]

    assert "affiliation" not in str(parsed.values)
    assert parsed.raw["authors"][0]["affiliation"] == "Example University", "kept in raw"


def test_unstructured_ieee_fields_stay_in_raw() -> None:
    parsed = importer.parse_records(IEEE_SAMPLE)[0]

    assert parsed.raw["citing_paper_count"] == 12
    assert parsed.raw["download_count"] == 99
    assert parsed.raw["license"] == "IEEE"
    assert parsed.raw["rank"] == 1
    assert "citing_paper_count" not in parsed.values
    assert parsed.fetched_at is not None


def test_source_ref_prefers_the_doi() -> None:
    assert importer.parse_records(IEEE_SAMPLE)[0].source_ref == f"doi:{DOI.casefold()}"


def test_source_ref_falls_back_to_the_ieee_number() -> None:
    record = dict(IEEE_SAMPLE["articles"][0])
    record.pop("doi")

    parsed = importer.parse_records({"articles": [record]})[0]

    assert parsed.source_ref == "ieee:7065247"


def test_source_ref_falls_back_to_the_file_and_then_the_digest() -> None:
    record = {"title": "T", "path": "/tmp/a.pdf", "sha256": "a" * 64}

    assert importer.parse_records([record])[0].source_ref == f"file:/tmp/a.pdf:{'a' * 64}"
    assert importer.parse_records([{"title": "T"}])[0].source_ref.startswith("record:")


def test_authors_are_sorted_by_author_order() -> None:
    record = dict(IEEE_SAMPLE["articles"][0])
    record["authors"] = [
        {"full_name": "Second Author", "author_order": 2},
        {"full_name": "First Author", "author_order": 1},
    ]

    parsed = importer.parse_records({"articles": [record]})[0]

    assert parsed.authors == ["First Author", "Second Author"]


def test_csl_json_is_parsed() -> None:
    parsed = importer.parse_records(
        [
            {
                "DOI": "10.1234/abc",
                "type": "paper-conference",
                "title": "A conference paper",
                "author": [{"given": "Alice", "family": "Smith"}, {"literal": "Example Group"}],
                "issued": {"date-parts": [[2019, 5, 1]]},
                "container-title": "Proceedings of ExampleConf",
                "volume": "3",
                "issue": "1",
                "page": "10-20",
                "ISSN": "1234-5678",
                "URL": "https://example.org/paper",
                "abstract": "An abstract",
                "keyword": "one, two",
            }
        ]
    )[0]

    assert parsed.format == importer.FORMAT_CSL_JSON
    assert parsed.authors == ["Alice Smith", "Example Group"]
    assert parsed.year == 2019
    assert parsed.values["paper_type"] == "conference"
    assert parsed.values["venue"] == {
        "name": "Proceedings of ExampleConf",
        "year": 2019,
        "issn": "1234-5678",
    }
    assert parsed.values["pages"] == "10-20"
    assert parsed.values["tag:author_terms"] == ["one", "two"]


def test_the_generic_shape_accepts_aliases() -> None:
    parsed = importer.parse_records(
        [{"title": "T", "doi": "10.1/x", "year": "2015", "journal": "ISSCC", "keywords": ["a"]}]
    )[0]

    assert parsed.format == importer.FORMAT_GENERIC
    assert parsed.values["identifier:doi"] == "10.1/x"
    assert parsed.values["year"] == 2015
    assert parsed.values["venue"] == "ISSCC"
    assert parsed.values["tag:author_terms"] == ["a"]


def test_load_payload_tolerates_a_bom() -> None:
    assert importer.load_payload('﻿{"articles": []}') == {"articles": []}
    assert importer.load_payload(b'[{"title": "T"}]') == [{"title": "T"}]


def test_an_unsupported_payload_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        importer.detect_format("just a string")


# --------------------------------------------------------------------------- #
# the import: dry run
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing_but_reports_the_match(db_session) -> None:
    paper = paper_with_doi(db_session)
    before = (paper.title, paper.volume, paper.issue)

    report = importer.import_payload(db_session, IEEE_SAMPLE)

    assert report.as_dict()["total"] == 1
    assert report.matched == 1
    assert report.dry_run is True
    assert report.sources[0]["paper_id"] == paper.id
    assert report.sources[0]["match_method"] == "doi"
    assert (paper.title, paper.volume, paper.issue) == before
    assert db_session.query(PaperSource).count() == 0
    assert db_session.query(Venue).count() == 0


def test_dry_run_reports_a_shell_that_would_be_created(db_session) -> None:
    report = importer.import_payload(db_session, IEEE_SAMPLE)

    assert report.created_shell == 1
    assert report.matched == 0
    assert report.sources[0]["note"].startswith("would create a shell")
    assert db_session.query(Paper).count() == 0


def test_dry_run_reports_conflicts_without_writing(db_session) -> None:
    paper = paper_with_doi(db_session, title="Heuristic title", volume="1")
    seed_heuristic_title(db_session, paper, "Heuristic title")
    provenance_service.set_field(db_session, paper, "volume", "1", override=True)

    report = importer.import_payload(db_session, IEEE_SAMPLE)

    fields = {item["field"] for item in report.conflicts}
    assert "volume" in fields, "volume already had a value from an unclaimed source"
    assert paper.volume == "1"


# --------------------------------------------------------------------------- #
# the import: apply
# --------------------------------------------------------------------------- #
def test_apply_fills_the_record_onto_the_matched_paper(db_session) -> None:
    paper = paper_with_doi(db_session, title="Heuristic title")
    seed_heuristic_title(db_session, paper, "Heuristic title")

    report = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert report.matched == 1
    assert report.dry_run is False
    assert paper.volume == "62"
    assert paper.issue == "7"
    assert paper.pages == "631-635"
    assert paper.paper_type == "journal"
    assert paper.publication_date.isoformat() == "2015-07-01"
    assert paper.title == "A 0.6 V Low Power SRAM with Leakage Reduction"
    assert paper_service.paper_author_names(paper) == ["Alice Smith", "Bob Jones"]
    assert paper.url == "https://ieeexplore.ieee.org/document/7065247"


def test_apply_records_the_venue_and_its_edition(db_session) -> None:
    paper = paper_with_doi(db_session)
    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    venue = db_session.query(Venue).one()
    edition = db_session.query(VenueEdition).one()

    assert venue.name == "IEEE Journal of Solid-State Circuits"
    assert venue.kind == "journal"
    assert venue.issn == "0018-9219"
    assert edition.year == 2015
    assert edition.publication_number == "12345"
    assert edition.location == "San Francisco, CA, USA"
    assert paper.venue_id == venue.id
    assert paper.venue_edition_id == edition.id
    assert paper.venue_year == 2015


def test_apply_stores_the_three_kinds_of_index_terms(db_session) -> None:
    paper = paper_with_doi(db_session)

    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert tags.tags_for_paper(db_session, paper, kind="ieee_terms") == [
        "SRAM",
        "low-power electronics",
    ]
    assert tags.tags_for_paper(db_session, paper, kind="author_terms") == ["leakage reduction"]
    assert tags.tags_for_paper(db_session, paper, kind="dynamic_index_terms") == [
        "subthreshold operation"
    ]
    grouped = tags.tags_by_kind(db_session, paper)
    assert set(grouped) == {"ieee_terms", "author_terms", "dynamic_index_terms"}


def test_apply_stores_the_raw_record(db_session) -> None:
    paper = paper_with_doi(db_session)

    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    source = sources.find_source(
        db_session, sources.SOURCE_TYPE_IMPORT_FILE, f"doi:{DOI.casefold()}"
    )
    assert source is not None
    assert source.raw["article_number"] == "7065247"
    assert source.content_type == "Journals"
    assert source.match_method == "doi"
    assert source.match_confidence == 1.0
    assert source.paper_id == paper.id
    assert source.importer == "metadata_import"


def test_apply_registers_the_identifiers_and_upgrades_the_fingerprint(db_session) -> None:
    """A paper matched by title/author/year gains the DOI *and* a better fingerprint."""
    paper = make_paper(
        db_session,
        title="A 0.6 V Low Power SRAM with Leakage Reduction",
        year=2015,
    )
    paper_service.set_paper_authors(db_session, paper, ["Alice Smith", "Bob Jones"])
    before = paper.fingerprint
    assert before.startswith("sha256:")

    report = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert report.matched == 1
    rows = {row.scheme: row for row in ids.identifiers_for_paper(db_session, paper.id)}
    assert set(rows) == {"doi", "ieee_article_number", "issn"}
    assert rows["doi"].is_primary is True
    assert rows["issn"].is_primary is False
    assert paper.doi == DOI.casefold()
    assert paper.fingerprint == f"doi:{DOI.casefold()}"


def test_the_structured_source_overrides_the_heuristic_title(db_session) -> None:
    paper = paper_with_doi(db_session, title="Heuristic title")
    seed_heuristic_title(db_session, paper, "Heuristic title")

    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    current = provenance_service.current_claim(db_session, paper.id, "title")
    assert current.value == "A 0.6 V Low Power SRAM with Leakage Reduction"
    assert current.decided_by == provenance_service.DECIDED_STRUCTURED_OVERRIDE
    history = provenance_service.field_history(db_session, paper.id, "title")
    assert "Heuristic title" in [row.value for row in history if not row.is_current]


def test_re_importing_the_same_record_changes_nothing(db_session) -> None:
    paper = paper_with_doi(db_session)

    first = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)
    second = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert first.matched == 1
    assert second.unchanged == 1
    assert second.matched == 0
    assert db_session.query(PaperSource).count() == 1
    assert db_session.query(Venue).count() == 1
    assert db_session.query(Paper).count() == 1


def test_an_unmatched_record_becomes_a_shell_paper(db_session) -> None:
    report = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert report.created_shell == 1
    shell = db_session.query(Paper).one()
    assert shell.status == paper_service.STATUS_AWAITING_FILE
    assert shell.title == "A 0.6 V Low Power SRAM with Leakage Reduction"
    assert shell.doi == DOI.casefold()
    assert shell.volume == "62"
    assert report.sources[0]["paper_id"] == shell.id
    assert report.sources[0]["match_method"] == "shell"


def test_import_is_idempotent_for_a_shell_too(db_session) -> None:
    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    again = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert again.unchanged == 1
    assert db_session.query(Paper).count() == 1


def test_a_title_only_record_goes_to_the_review_queue(db_session) -> None:
    make_paper(db_session, title="A 0.6 V Low Power SRAM with Leakage Reduction", year=2015)
    record = dict(IEEE_SAMPLE["articles"][0])
    record.pop("doi")
    record.pop("article_number")
    record.pop("authors")
    record.pop("publication_year")
    record.pop("publication_date")
    record.pop("issn")
    record.pop("start_page")
    record.pop("end_page")

    report = importer.import_payload(db_session, {"articles": [record]}, apply=True)

    assert report.ambiguous == 1
    assert report.matched == 0
    queued = sources.review_queue(db_session)
    assert len(queued) == 1
    assert queued[0].match_status == sources.MATCH_STATUS_AMBIGUOUS
    assert queued[0].paper_id is None


def test_the_limit_is_respected(db_session) -> None:
    record = IEEE_SAMPLE["articles"][0]
    payload = {"articles": [record, {**record, "doi": "10.1/second"}]}

    report = importer.import_payload(db_session, payload, apply=True, limit=1)

    assert report.total == 1
    assert db_session.query(PaperSource).count() == 1


def test_a_manual_source_type_can_be_imported_as_well(db_session) -> None:
    paper = paper_with_doi(db_session)

    report = importer.import_payload(
        db_session,
        IEEE_SAMPLE,
        apply=True,
        source_type=sources.SOURCE_TYPE_MANUAL,
    )

    assert report.matched == 1
    source = sources.find_source(db_session, sources.SOURCE_TYPE_MANUAL, f"doi:{DOI.casefold()}")
    assert source is not None
    assert merge.is_structured(source.source_type) is True

def test_reimport_after_soft_delete_repoints_the_record(db_session) -> None:
    """P1-6: 删除即释放必须延伸到来源记录——软删论文的 (source_type, source_ref)
    不能把同一份记录永远钉在 "unchanged" 上。"""
    importer.import_payload(db_session, IEEE_SAMPLE, apply=True)
    original = db_session.query(Paper).first()

    paper_service.soft_delete_paper(db_session, original)
    db_session.flush()
    # 模拟「删论文 → 重传 PDF」：身份已释放，新论文重新占用同一个 DOI。
    replacement = paper_with_doi(db_session, title=original.title, year=original.year)

    again = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)

    assert again.unchanged == 0
    assert again.matched == 1
    rows = db_session.query(PaperSource).all()
    assert len(rows) == 1, "the re-import must re-point, not create a second row"
    assert rows[0].paper_id == replacement.id

    third = importer.import_payload(db_session, IEEE_SAMPLE, apply=True)
    assert third.unchanged == 1  # idempotent again, now against the new paper


# --------------------------------------------------------------------------- #
# 会议日期区间（2026-10-10 回归）
#
# 真实 IEEE 批次的 ``publication_date`` 是**区间**：``"17-19 Oct. 2025"``。原实现
# 在识别到月份名后把字符串里所有数字拼起来当年份 → ``17192025`` → ``date()`` 抛
# ``ValueError: year 18202014 is out of range``，于是**整个文件被 422 拒收**。
# ieee_tools 的两份真实数据里 tdadc 750/1103、tdc 3808/5933 条会踩到这个。
# --------------------------------------------------------------------------- #

#: (原始字符串, 期望年份, 期望的月精度日期)
IEEE_DATE_RANGES = [
    ("17-19 Oct. 2025", 2025, "2025-10-01"),
    ("3-5 Dec. 2012", 2012, "2012-12-01"),
    ("14-18 Sept. 2015", 2015, "2015-09-01"),
    ("18-20 June 2014", 2014, "2014-06-01"),
    ("11-16 Nov. 2006", 2006, "2006-11-01"),
    ("3-3 Oct. 2013", 2013, "2013-10-01"),
    ("1-5 June 2014", 2014, "2014-06-01"),
]


def test_a_conference_date_range_does_not_blow_up_the_parse() -> None:
    """年份是那个独立的四位数字，不是把区间里的数字拼起来。"""
    for raw, year, iso in IEEE_DATE_RANGES:
        record = dict(IEEE_SAMPLE["articles"][0])
        record["publication_date"] = raw
        record["publication_year"] = year          # 真实 IEEE 记录两个字段都给
        parsed = importer.parse_record(record, importer.FORMAT_IEEE_RAW)
        assert parsed.year == year, raw
        assert parsed.values["publication_date"] == iso, raw


def test_a_date_range_still_yields_a_year_without_publication_year() -> None:
    """``publication_year`` 缺失时年份要从日期串里读出来 —— 这条路径以前会返回 None。"""
    for raw, year, iso in IEEE_DATE_RANGES:
        record = dict(IEEE_SAMPLE["articles"][0])
        record["publication_date"] = raw
        record.pop("publication_year", None)
        parsed = importer.parse_record(record, importer.FORMAT_IEEE_RAW)
        assert parsed.year == year, raw
        assert parsed.values["publication_date"] == iso, raw


def test_a_whole_payload_of_date_ranges_imports_instead_of_raising() -> None:
    """一份全是日期区间的载荷必须能被读进去（以前第一条就抛 ValueError）。"""
    payload = {
        "total_records": len(IEEE_DATE_RANGES),
        "articles": [
            dict(
                IEEE_SAMPLE["articles"][0],
                doi=f"10.1109/TEST.{index}",
                publication_date=raw,
                publication_year=year,
            )
            for index, (raw, year, _iso) in enumerate(IEEE_DATE_RANGES)
        ],
    }
    detected, records = importer.records_from_payload(payload)
    assert detected == importer.FORMAT_IEEE_RAW
    parsed = [importer.parse_record(record, detected) for record in records]
    assert len(parsed) == len(IEEE_DATE_RANGES)
    assert [p.year for p in parsed] == [year for _raw, year, _iso in IEEE_DATE_RANGES]


def test_month_precision_without_a_year_keeps_the_old_behaviour() -> None:
    """没有年份时保持原样：认得出月份就给年初以外的月精度，给不出就回落。"""
    record = dict(IEEE_SAMPLE["articles"][0])
    record["publication_date"] = "July 2015"
    assert importer.parse_record(record, importer.FORMAT_IEEE_RAW).values["publication_date"] == "2015-07-01"

    record = dict(IEEE_SAMPLE["articles"][0])
    record["publication_date"] = "2015-08-09"
    assert importer.parse_record(record, importer.FORMAT_IEEE_RAW).values["publication_date"] == "2015-01-01"

    record = dict(IEEE_SAMPLE["articles"][0])
    record["publication_date"] = "no digits here"
    record.pop("publication_year", None)
    assert "publication_date" not in importer.parse_record(
        record, importer.FORMAT_IEEE_RAW
    ).values


# --------------------------------------------------------------------------- #
# 单条解析失败不再拖垮整批（2026-10-10，owner 口径）
#
# 解析器本身对畸形输入很稳（dict/list/None 进 date/authors 都不抛，见
# tmp/probe_bad_records.py 的探测），所以这里用**注入式失败**来确定性地验证容错层：
# 要求是"跳过这一条、其余照常、并在回执里点名 + 给原因"。
# --------------------------------------------------------------------------- #

from unittest import mock  # noqa: E402


def _payload_with_one_bad_record(count: int = 4) -> dict:
    articles = [
        dict(IEEE_SAMPLE["articles"][0], doi=f"10.1109/TEST.{index}")
        for index in range(count)
    ]
    broken = min(2, count - 1)
    articles[broken] = dict(articles[broken], doi="10.1109/TEST.BROKEN")
    return {"total_records": count, "articles": articles}


def test_a_single_unparseable_record_is_skipped_and_named(db_session) -> None:
    payload = _payload_with_one_bad_record()
    real_parse = importer.parse_record

    def explode(record, fmt):
        if record.get("doi") == "10.1109/TEST.BROKEN":
            raise ValueError("year 18202014 is out of range")
        return real_parse(record, fmt)

    with mock.patch.object(importer, "parse_record", side_effect=explode):
        report = importer.import_payload(db_session, payload, apply=False)

    assert report.detected == 4, "检测到的条目数要含失败那条"
    assert report.failed == 1
    assert report.total == 3, "其余三条照常进入匹配"
    assert report.total + report.failed + report.skipped == report.detected
    assert len(report.failures) == 1
    failure = report.failures[0]
    assert failure["index"] == 2
    assert failure["identifier"] == "10.1109/TEST.BROKEN"
    assert "year 18202014 is out of range" in failure["reason"]
    assert failure["reason"].startswith("ValueError")


def test_a_payload_where_every_record_fails_still_returns_a_report(db_session) -> None:
    payload = _payload_with_one_bad_record(count=3)
    with mock.patch.object(
        importer, "parse_record", side_effect=ValueError("cannot read this record")
    ):
        report = importer.import_payload(db_session, payload, apply=False)

    assert report.detected == 3
    assert report.total == 0
    assert report.failed == 3
    assert len(report.failures) == 3


def test_the_failure_entry_falls_back_to_the_title_when_there_is_no_doi() -> None:
    from app.services.metadata_import import _record_hint

    assert _record_hint({"doi": "10.1/x"}) == "10.1/x"
    assert _record_hint({"title": "A paper"}) == "A paper"
    assert _record_hint({"author": "someone"}) is None
    assert _record_hint("not a mapping") is None


def test_the_report_arithmetic_holds_when_a_limit_is_applied(db_session) -> None:
    payload = _payload_with_one_bad_record(count=5)
    real_parse = importer.parse_record

    def explode(record, fmt):
        if record.get("doi") == "10.1109/TEST.BROKEN":
            raise ValueError("boom")
        return real_parse(record, fmt)

    with mock.patch.object(importer, "parse_record", side_effect=explode):
        report = importer.import_payload(db_session, payload, apply=False, limit=2)

    assert report.detected == 5
    assert report.failed == 1
    assert report.total == 2
    assert report.skipped == 2, "剩下两条是被 limit 截断的"
    assert report.total + report.failed + report.skipped == report.detected


def test_the_strict_parser_still_raises_for_callers_that_want_that(db_session) -> None:
    """``parse_records``（严格版）保持抛异常：attach/apply 依赖它单条读取已存记录。"""
    payload = _payload_with_one_bad_record(count=2)
    real_parse = importer.parse_record

    def explode(record, fmt):
        raise ValueError("nope")

    with mock.patch.object(importer, "parse_record", side_effect=explode):
        import pytest

        with pytest.raises(ValueError):
            importer.parse_records(payload)
        del real_parse
