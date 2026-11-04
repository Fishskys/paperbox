"""Publication identity must not be inferred from delivery boilerplate."""

import pytest

from app.parsing.metadata_fields import clean_title
from app.parsing.pdf import EmbeddedMetadata, PageText, SizedLine
from app.services.metadata_service import detect_title, detect_year, embedded_claim_values


@pytest.mark.parametrize("watermark", [
    "Authorized licensed use limited to: University. Downloaded on November 01,2026 at 09:00:00 UTC from IEEE Xplore. Restrictions apply.",
    "Downloaded on November 01,2026 at 09:00:00 UTC from IEEE Xplore.",
])
def test_download_year_does_not_replace_publication_year(watermark):
    pages = [PageText(page=1, text=f"A Timing Circuit\n978-1-2345 © 1996 IEEE\n{watermark}"),
             PageText(page=2, text=f"Measurements since 2020\n{watermark}")]
    assert detect_year(pages) == 1996


def test_references_and_ambiguous_body_years_are_not_publication_evidence():
    assert detect_year([PageText(page=1, text="Experiments in 2015 and 2016.\nReferences\n[1] Circuit, 2025")]) is None
    assert detect_year([PageText(page=1, text="Downloaded on Nov 01,2026\nRestrictions apply.")]) is None


def test_arxiv_and_publication_header_years():
    assert detect_year([], "https://arxiv.org/abs/1706.03762") == 2017
    assert detect_year([PageText(page=1, text="IEEE SOLID-STATE CIRCUITS LETTERS, VOL. 2, NO. 9, SEPTEMBER 2019")]) == 2019


def test_conflicting_publication_evidence_is_not_guessed():
    assert detect_year([PageText(page=1, text="2019 IEEE Conference\nCopyright 2020 IEEE")]) is None


def test_title_cleanup_does_not_rewrite_identifiers_or_authors():
    embedded = EmbeddedMetadata(title="A <i>CMOS</i> Circuit", doi="10.1109/example.2019.123", authors=["G. Avolio"])
    values = embedded_claim_values(embedded)
    assert values["identifier:doi"] == embedded.doi
    assert values["authors"] == embedded.authors


def test_journal_running_header_is_not_title_even_with_larger_font():
    header = "IEEE SOLID-STATE CIRCUITS LETTERS, VOL. 2, NO. 9, SEPTEMBER 2019"
    title = "A Low Power Time to Digital Converter"
    pages = [PageText(page=1, text=f"{header}\n{title}\nAlice Smith and Bob Jones")]
    assert detect_title(pages) == title
    assert detect_title(pages, [SizedLine(text=header, size=18), SizedLine(text=title, size=16)]) == title


def test_missing_text_title_does_not_turn_byline_or_affiliation_into_identity():
    lines = ["Dieter Genschow, Johannes Kloas", "IHP, Im Technologieark 25, Frankfurt, Germany", "Abstract—This paper evaluates a radar interface"]
    pages = [PageText(page=1, text="\n".join(lines))]
    assert detect_title(pages) is None
    assert detect_title(pages, [SizedLine(text=line, size=11) for line in lines]) is None
    title = "Low Power Circuits, Accurate Digital Timing"
    assert detect_title([PageText(page=1, text=title + "\n" + "\n".join(lines))]) == title
    assert detect_title([PageText(page=1, text="Noise Shaping and Timing Conversion\n" + "\n".join(lines))]) == "Noise Shaping and Timing Conversion"


@pytest.mark.parametrize(("raw", "expected"), [
    ('A <inline-formula> <tex-math notation="LaTeX">$16\\times8$ </tex-math></inline-formula> Digital-SiPM Array', 'A 16×8 Digital-SiPM Array'),
    ('A 40-<inline-formula><tex-math>$\\mu$</tex-math></inline-formula>m Circuit', 'A 40-μm Circuit'),
    ('A <inline-formula><tex-math>$V$</tex-math></inline-formula><sub>MAX</sub>-Compliant Circuit', 'A VMAX-Compliant Circuit'),
    ('Signals &amp; Noise in <i>CMOS</i>', 'Signals & Noise in CMOS'),
    ('An <italic xmlns:x="urn:x">italic</italic> N', 'An italic N'),
    (r'An Unknown $\custom{x}$ Circuit', r'An Unknown $\custom{x}$ Circuit'),
])
def test_presentation_cleanup_preserves_unknown_math(raw, expected):
    assert clean_title(raw) == expected
    embedded = EmbeddedMetadata(title=raw)
    assert embedded_claim_values(embedded)["title"] == expected
    assert embedded.title == raw