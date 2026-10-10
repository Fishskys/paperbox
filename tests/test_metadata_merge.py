"""Merge rule R2: fill blanks, override heuristics, never rank structured sources.

The three branches the plan calls out explicitly:

* fill a blank,
* structured source overrides ``pdf_heuristic``,
* structured vs. structured -> keep the current value and record the conflict.

Plus the field specifics (``abstract`` longest, ``authors`` longest list,
``year`` keeps the current value) and ``manual`` being exempt from R2.
"""

from __future__ import annotations

from app.db.models import Paper, PaperSource, new_uuid
from app.services import metadata_merge as merge
from app.services import provenance_service as prov


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "Heuristic title",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def make_source(session, paper_id, source_type) -> PaperSource:
    source = PaperSource(
        id=new_uuid(),
        paper_id=paper_id,
        source_type=source_type,
        source_ref=f"{source_type}:{new_uuid()}",
        raw={},
        match_status="matched",
    )
    session.add(source)
    session.flush()
    return source


def seed(session, paper, field, value, source_type) -> prov.PaperFieldProvenance:
    """Put a value in place the way the named source would have."""
    source = make_source(session, paper.id, source_type)
    claim = prov.set_field(
        session, paper, field, value, source_id=source.id, override=True
    )
    return claim


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_structured_and_heuristic_classification() -> None:
    assert merge.is_structured("ieee_api")
    assert merge.is_structured("manual")
    assert not merge.is_structured("pdf_heuristic")
    assert not merge.is_structured(None)
    assert merge.is_heuristic("PDF_Heuristic")
    assert not merge.is_heuristic("ieee_api")


def test_source_type_of_an_unclaimed_value_is_the_heuristic(db_session) -> None:
    """Everything written before this layer came from the first-page heuristics."""
    paper = make_paper(db_session, title="Legacy")

    assert merge.current_source_type(db_session, paper, "title") == "pdf_heuristic"


def test_source_type_reads_the_claim_source(db_session) -> None:
    paper = make_paper(db_session)
    seed(db_session, paper, "title", "From IEEE", "ieee_api")

    assert merge.current_source_type(db_session, paper, "title") == "ieee_api"


# --------------------------------------------------------------------------- #
# 1. fill blanks
# --------------------------------------------------------------------------- #
def test_a_blank_field_is_filled(db_session) -> None:
    paper = make_paper(db_session, abstract=None)

    report = merge.merge_values(
        db_session, paper, {"abstract": "An abstract"}, source_type="ieee_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_FILLED]
    assert paper.abstract == "An abstract"


def test_a_blank_title_is_not_an_empty_string(db_session) -> None:
    paper = make_paper(db_session, title="   ")
    paper.title = "   "

    decision = merge.decide(db_session, paper, "title", "Real", source_type="ieee_api")

    assert decision.action == merge.ACTION_FILLED


def test_an_empty_incoming_value_is_ignored(db_session) -> None:
    paper = make_paper(db_session, volume=None)

    report = merge.merge_values(db_session, paper, {"volume": None}, source_type="ieee_api")

    assert [item.action for item in report.decisions] == [merge.ACTION_UNCHANGED]
    assert paper.volume is None


def test_restating_the_same_value_changes_nothing(db_session) -> None:
    paper = make_paper(db_session, title="Same")
    seed(db_session, paper, "title", "Same", "pdf_heuristic")

    report = merge.merge_values(db_session, paper, {"title": "Same"}, source_type="ieee_api")

    assert [item.action for item in report.decisions] == [merge.ACTION_UNCHANGED]


# --------------------------------------------------------------------------- #
# 2. the structured-over-heuristic exception
# --------------------------------------------------------------------------- #
def test_structured_source_overrides_a_heuristic_value(db_session) -> None:
    paper = make_paper(db_session, title="Heuristic title")
    seed(db_session, paper, "title", "Heuristic title", "pdf_heuristic")

    report = merge.merge_values(
        db_session, paper, {"title": "Real title"}, source_type="ieee_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_OVERRIDDEN]
    assert paper.title == "Real title"
    current = prov.current_claim(db_session, paper.id, "title")
    assert current.decided_by == prov.DECIDED_STRUCTURED_OVERRIDE
    assert current.value == "Real title"
    history = prov.field_history(db_session, paper.id, "title")
    assert [row.value for row in history if not row.is_current] == ["Heuristic title"]


def test_a_heuristic_does_not_override_a_heuristic(db_session) -> None:
    paper = make_paper(db_session, title="First heuristic")
    seed(db_session, paper, "title", "First heuristic", "pdf_heuristic")

    report = merge.merge_values(
        db_session, paper, {"title": "Second heuristic"}, source_type="pdf_heuristic"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert paper.title == "First heuristic"


def test_a_heuristic_does_not_override_a_structured_value(db_session) -> None:
    paper = make_paper(db_session, title="From IEEE")
    seed(db_session, paper, "title", "From IEEE", "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"title": "From a PDF"}, source_type="pdf_heuristic"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert paper.title == "From IEEE"


def test_legacy_values_without_provenance_are_overridable(db_session) -> None:
    """The 68 backfilled papers have heuristic claims; a bare column is the same."""
    paper = make_paper(db_session, title="Legacy column only")

    report = merge.merge_values(
        db_session, paper, {"title": "From IEEE"}, source_type="ieee_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_OVERRIDDEN]
    assert paper.title == "From IEEE"


# --------------------------------------------------------------------------- #
# 3. structured vs structured
# --------------------------------------------------------------------------- #
def test_two_structured_sources_keep_the_current_value_and_log_a_conflict(db_session) -> None:
    paper = make_paper(db_session, title="From IEEE")
    seed(db_session, paper, "title", "From IEEE", "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"title": "From arXiv"}, source_type="arxiv_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert paper.title == "From IEEE", "no authority ranking: first writer wins"
    assert prov.current_claim(db_session, paper.id, "title").value == "From IEEE"
    loser = [row for row in prov.field_history(db_session, paper.id, "title") if not row.is_current]
    assert [row.value for row in loser] == ["From arXiv"], "the disagreement is recorded"


def test_conflicts_are_reported_in_the_import_shape(db_session) -> None:
    paper = make_paper(db_session, title="From IEEE")
    seed(db_session, paper, "title", "From IEEE", "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"title": "From arXiv"}, source_type="arxiv_api"
    )
    conflicts = merge.conflict_report(report)

    assert conflicts == [
        {
            "field": "title",
            "kept": "From IEEE",
            "rejected": "From arXiv",
            "source": "arxiv_api",
            "reason": "keep the current value and record the disagreement",
        }
    ]


# --------------------------------------------------------------------------- #
# 4. field specifics
# --------------------------------------------------------------------------- #
def test_abstract_keeps_the_longest_text(db_session) -> None:
    paper = make_paper(db_session, abstract="short")
    seed(db_session, paper, "abstract", "short", "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"abstract": "a much longer abstract"}, source_type="arxiv_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_SPECIAL]
    assert paper.abstract == "a much longer abstract"


def test_abstract_from_a_heuristic_is_overridden_not_compared(db_session) -> None:
    """Rule 2 fires before the field special: a structured value always wins."""
    paper = make_paper(db_session, abstract="a much longer heuristic abstract")
    seed(db_session, paper, "abstract", "a much longer heuristic abstract", "pdf_heuristic")

    report = merge.merge_values(
        db_session, paper, {"abstract": "short"}, source_type="ieee_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_OVERRIDDEN]
    assert paper.abstract == "short"


def test_abstract_keeps_the_current_text_when_it_is_longer(db_session) -> None:
    long_text = "a much longer abstract"
    paper = make_paper(db_session, abstract=long_text)
    seed(db_session, paper, "abstract", long_text, "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"abstract": "short"}, source_type="arxiv_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert paper.abstract == long_text


def test_authors_keep_the_longest_list(db_session) -> None:
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice"], "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"authors": ["Alice", "Bob", "Carol"]}, source_type="arxiv_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_SPECIAL]
    assert prov.read_field(paper, "authors") == ["Alice", "Bob", "Carol"]


def test_authors_conflict_when_the_incoming_list_is_shorter(db_session) -> None:
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice", "Bob"], "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"authors": ["Alice"]}, source_type="arxiv_api"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert prov.read_field(paper, "authors") == ["Alice", "Bob"]


def test_year_conflict_keeps_the_current_value(db_session) -> None:
    paper = make_paper(db_session, year=2015)
    seed(db_session, paper, "year", 2015, "ieee_api")

    report = merge.merge_values(db_session, paper, {"year": 2016}, source_type="arxiv_api")

    assert [item.action for item in report.decisions] == [merge.ACTION_CONFLICT]
    assert paper.year == 2015


# --------------------------------------------------------------------------- #
# manual is exempt (decision 12)
# --------------------------------------------------------------------------- #
def test_manual_overrides_a_structured_value(db_session) -> None:
    paper = make_paper(db_session, title="From IEEE")
    seed(db_session, paper, "title", "From IEEE", "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"title": "Hand corrected"}, source_type="manual"
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_OVERRIDDEN]
    assert paper.title == "Hand corrected"
    assert prov.current_claim(db_session, paper.id, "title").decided_by == prov.DECIDED_MANUAL


# --------------------------------------------------------------------------- #
# reports and dry runs
# --------------------------------------------------------------------------- #
def test_dry_run_decides_without_writing(db_session) -> None:
    paper = make_paper(db_session, abstract=None, volume=None)

    report = merge.merge_values(
        db_session,
        paper,
        {"abstract": "An abstract", "volume": "62"},
        source_type="ieee_api",
        dry_run=True,
    )

    assert [item.action for item in report.decisions] == [merge.ACTION_FILLED] * 2
    assert paper.abstract is None and paper.volume is None
    assert prov.field_history(db_session, paper.id) == []


def test_venue_and_identifier_values_also_merge(db_session) -> None:
    paper = make_paper(db_session)

    report = merge.merge_values(
        db_session,
        paper,
        {
            "venue": {"name": "ISSCC", "year": 2015, "content_type": "Conferences"},
            "identifier:doi": "10.1109/JSSC.2020.1",
        },
        source_type="ieee_api",
    )

    assert {item.action for item in report.decisions} == {merge.ACTION_FILLED}
    assert paper.venue is not None and paper.venue_year == 2015
    assert paper.doi == "10.1109/jssc.2020.1"


def test_summary_counts_by_action(db_session) -> None:
    paper = make_paper(db_session, volume="62")

    report = merge.merge_values(
        db_session,
        paper,
        {"volume": "62", "issue": "7", "pages": "631-635"},
        source_type="ieee_api",
    )

    assert merge.decisions_summary(report.decisions) == {"unchanged": 1, "filled": 2}
    assert merge.field_values_from_paper(paper)["pages"] == "631-635"

# --------------------------------------------------------------------------- #
# 规则 4 的适用面（2026-10-10）：只在结构化来源之间按"更长/更多"定胜负
# --------------------------------------------------------------------------- #
def test_a_heuristic_list_cannot_outgrow_a_structured_one(db_session) -> None:
    """真机事故（2604.01520）：PDF 启发式把标题碎片当作者，列表更长就赢了内嵌元数据。

    修复前 `authors` 走规则 4「取最长列表」，而启发式的错抽列表恰好更长（10 项含
    `Collaborative Platform`），于是盖住了内嵌 PDF 的干净 7 个人名。
    """
    paper = make_paper(db_session)
    clean = ["Lei Wang", "Yuanzi Li", "Jinchao Wu"]
    seed(db_session, paper, "authors", clean, "pdf_embedded")

    junk = ["Collaborative Platform", "for Social", *clean]
    report = merge.merge_values(
        db_session, paper, {"authors": junk}, source_type="pdf_heuristic"
    )

    decision = report.decisions[0]
    assert decision.action == merge.ACTION_CONFLICT
    assert decision.reason == "a weak source cannot outgrow a structured one"
    assert prov.read_field(paper, "authors") == clean, "干净的那份必须留下"


def test_a_heuristic_abstract_cannot_outgrow_a_structured_one(db_session) -> None:
    """`abstract` 同理：启发式读到的是整页乱码，比结构化摘要长。"""
    paper = make_paper(db_session)
    seed(db_session, paper, "abstract", "Short but structured.", "ieee_api")

    report = merge.merge_values(
        db_session,
        paper,
        {"abstract": "Short but structured. " + "junk " * 50},
        source_type="pdf_heuristic",
    )

    assert report.decisions[0].action == merge.ACTION_CONFLICT
    assert prov.read_field(paper, "abstract") == "Short but structured."


def test_two_weak_sources_keep_the_current_value(db_session) -> None:
    """规则 3 说弱来源之间不比较可信度 —— 规则 4 也不该替它们比长度。

    否则修好抽取器后重新解析也救不回来：旧的那份垃圾更长，永远赢。
    """
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice", "Bob"], "pdf_heuristic")

    report = merge.merge_values(
        db_session, paper, {"authors": ["Alice"]}, source_type="pdf_heuristic"
    )

    assert report.decisions[0].action == merge.ACTION_CONFLICT
    assert report.decisions[0].reason == "two weak sources disagree; the current value stays"
    assert prov.read_field(paper, "authors") == ["Alice", "Bob"]


def test_structured_sources_still_let_the_longer_list_win(db_session) -> None:
    """回归护栏：规则 4 在结构化来源之间照旧生效（两个真实 API 的列表长度不同）。"""
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice"], "ieee_api")

    report = merge.merge_values(
        db_session, paper, {"authors": ["Alice", "Bob", "Carol"]}, source_type="arxiv_api"
    )

    assert report.decisions[0].action == merge.ACTION_SPECIAL
    assert prov.read_field(paper, "authors") == ["Alice", "Bob", "Carol"]
