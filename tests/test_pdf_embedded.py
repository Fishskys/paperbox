"""PDF embedded metadata: the Info dictionary and the XMP packet (layer 1).

Layer 1 is the only discovery step that needs no network and no external service,
so it must work on every PDF and must never raise on a broken one.
"""

from __future__ import annotations

import io

from pypdf import PdfWriter

from app.parsing import pdf as pdf_module
from app.parsing.pdf import extract_embedded_metadata

XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>
   <dc:creator><rdf:Seq><rdf:li>{author_one}</rdf:li><rdf:li>{author_two}</rdf:li></rdf:Seq></dc:creator>
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{description}</rdf:li></rdf:Alt></dc:description>
   <dc:language><rdf:Bag><rdf:li>en</rdf:li></rdf:Bag></dc:language>
   <pdf:Keywords>{keywords}</pdf:Keywords>
   <prism:doi>{doi}</prism:doi>
   <prism:publicationName>{venue}</prism:publicationName>
   <prism:volume>{volume}</prism:volume>
   <prism:number>{issue}</prism:number>
   <prism:startingPage>{start_page}</prism:startingPage>
   <prism:endingPage>{end_page}</prism:endingPage>
   <prism:publicationDate>{publication_date}</prism:publicationDate>
   <prism:aggregationType>journal</prism:aggregationType>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def build_pdf(info: dict[str, str] | None = None, xmp: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if info:
        writer.add_metadata(info)
    if xmp:
        writer.xmp_metadata = xmp.encode("utf-8")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_info_dictionary_is_read() -> None:
    data = build_pdf(
        {
            "/Title": "Low Power SRAM Leakage Reduction",
            "/Author": "Alice Smith; Bob Jones",
            "/Subject": "A study of leakage in 28nm SRAM. doi:10.1109/JSSC.2020.1234567",
            "/Keywords": "SRAM, leakage, low power",
            "/CreationDate": "D:20150712090000Z",
        }
    )

    metadata = extract_embedded_metadata(data)

    assert metadata.title == "Low Power SRAM Leakage Reduction"
    assert metadata.authors == ["Alice Smith", "Bob Jones"]
    assert metadata.abstract.startswith("A study of leakage")
    # P1-10: a DOI *mentioned* in the abstract is a citation to another paper,
    # not this paper's identity -- free-text guessing is gone.
    assert metadata.doi is None
    assert metadata.keywords == ["SRAM", "leakage", "low power"]
    assert metadata.year == 2015
    assert metadata.is_empty is False


def test_xmp_packet_is_read_field_by_field() -> None:
    data = build_pdf(
        xmp=XMP_TEMPLATE.format(
            title="Sub-threshold SRAM design",
            author_one="Carol White",
            author_two="Dan Black",
            description="XMP abstract text",
            keywords="SRAM,sub-threshold",
            doi="10.1109/JSSC.2016.7654321",
            venue="IEEE Journal of Solid-State Circuits",
            volume="51",
            issue="4",
            start_page="880",
            end_page="892",
            publication_date="2016-04-01",
        )
    )

    metadata = extract_embedded_metadata(data)

    assert metadata.title == "Sub-threshold SRAM design"
    assert metadata.authors == ["Carol White", "Dan Black"]
    assert metadata.abstract == "XMP abstract text"
    assert metadata.doi == "10.1109/jssc.2016.7654321".replace("jssc", "JSSC")
    assert metadata.venue == "IEEE Journal of Solid-State Circuits"
    assert metadata.volume == "51"
    assert metadata.issue == "4"
    assert metadata.pages == "880-892"
    assert metadata.publication_date == "2016-04-01"
    assert metadata.year == 2016
    assert metadata.language == "en"
    assert metadata.keywords == ["SRAM", "sub-threshold"]
    assert metadata.raw["xmp"]["aggregationType"] == "journal"
    assert metadata.raw["xmp"]["doi"] == "10.1109/JSSC.2016.7654321"


def test_xmp_wins_over_the_info_dictionary() -> None:
    data = build_pdf(
        info={"/Title": "Producer title", "/Author": "Nobody"},
        xmp=XMP_TEMPLATE.format(
            title="Real title",
            author_one="Alice Smith",
            author_two="Bob Jones",
            description="XMP abstract",
            keywords="a,b",
            doi="10.1/x",
            venue="ISSCC",
            volume="62",
            issue="7",
            start_page="631",
            end_page="635",
            publication_date="July 2015",
        ),
    )

    metadata = extract_embedded_metadata(data)

    assert metadata.title == "Real title"
    assert metadata.authors == ["Alice Smith", "Bob Jones"]
    assert metadata.venue == "ISSCC"
    assert metadata.year == 2015


def test_an_arxiv_id_in_the_keywords_is_not_claimed() -> None:
    """P1-10: keywords/abstract mention OTHER papers' ids all the time; the
    corpus scan (30 papers) found zero real stamps in Info/XMP free text --
    arXiv identity comes from the page-text heuristic layer and file names."""
    data = build_pdf(
        {
            "/Title": "Attention Is All You Need",
            "/Keywords": "arXiv:1706.03762v5 transformer",
        }
    )

    metadata = extract_embedded_metadata(data)

    assert metadata.arxiv_id is None


def test_a_pdf_without_any_metadata_is_empty_not_an_error() -> None:
    metadata = extract_embedded_metadata(build_pdf())

    assert metadata.is_empty is True
    assert metadata.title is None
    assert metadata.doi is None
    assert metadata.authors == []
    assert "info" in metadata.raw and "xmp" in metadata.raw


def test_broken_input_never_raises() -> None:
    for payload in (b"", b"not a pdf at all"):
        metadata = extract_embedded_metadata(payload)

        assert metadata.is_empty is True


def test_as_dict_keeps_only_real_values() -> None:
    data = build_pdf({"/Title": "Only a title"})

    payload = extract_embedded_metadata(data).as_dict()

    assert payload["title"] == "Only a title"
    assert "doi" not in payload
    assert "raw" in payload


def test_issn_and_isbn_are_kept_in_the_raw_snapshot() -> None:
    data = build_pdf(
        {
            "/Title": "A journal article",
            "/Subject": "ISSN 0018-9219, ISBN 978-3-16-148410-0",
        }
    )

    raw = extract_embedded_metadata(data).raw

    assert raw["issns"] == ["0018-9219"]
    assert raw["isbns"]


def test_pages_fall_back_to_the_start_page_alone() -> None:
    xmp = XMP_TEMPLATE.format(
        title="T",
        author_one="A",
        author_two="B",
        description="d",
        keywords="k",
        doi="10.1/x",
        venue="v",
        volume="1",
        issue="2",
        start_page="10",
        end_page="",
        publication_date="2015",
    )

    assert extract_embedded_metadata(build_pdf(xmp=xmp)).pages == "10"


def test_module_exposes_the_public_helper() -> None:
    assert pdf_module.extract_embedded_metadata is extract_embedded_metadata