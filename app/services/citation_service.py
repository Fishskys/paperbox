"""Read-only citation rendering from current library metadata."""

import re

from sqlalchemy.orm import Session

from app.parsing.metadata_fields import clean_title
from app.services import paper_service

_TEX = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
_TYPES = {"journal": ("article", "JOUR"), "conference": ("inproceedings", "CONF"), "book": ("book", "BOOK"), "preprint": ("misc", "UNPB"), "standard": ("misc", "STAND")}


def _line(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _tex(value: str, *, literal: bool = False) -> str:
    replacements = _TEX if not literal else {key: value for key, value in _TEX.items() if key in "\\{}"}
    return "".join(replacements.get(character, character) for character in value)


def export(session: Session, paper_id: str, format: str) -> tuple[str, str, str]:
    paper = paper_service.get_paper(session, paper_id)
    if paper is None:
        raise LookupError("paper not found")
    title = clean_title(paper.title or "")
    authors = [_line(name) for name in paper_service.paper_author_names(paper) if _line(name)]
    venue = _line(paper.venue.name) if paper.venue else ""
    year = _line(paper.year)
    doi = _line(paper.doi)
    kind, ris_kind = _TYPES.get((paper.paper_type or "").casefold(), ("misc", "GEN"))
    if format == "text":
        parts = [", ".join(authors), title, venue, year]
        if paper.volume:
            parts.append(_line(paper.volume) + (f"({_line(paper.issue)})" if paper.issue else ""))
        if paper.pages:
            parts.append(f"pp. {_line(paper.pages)}")
        if doi:
            parts.append(f"doi:{doi}")
        return ". ".join(part.rstrip(".") for part in parts if part) + ".", "text/plain", "txt"
    if format == "bibtex":
        fields = {"title": "{" + _tex(title) + "}"} if title else {}
        if authors:
            fields["author"] = " and ".join("{" + _tex(name) + "}" if re.search(r"\band\b", name, re.I) else _tex(name) for name in authors)
        if venue:
            fields["journal" if kind == "article" else "booktitle" if kind == "inproceedings" else "howpublished"] = _tex(venue)
        for key, value in (("year", year), ("volume", paper.volume), ("number", paper.issue), ("pages", paper.pages), ("doi", doi), ("url", paper.url)):
            if value:
                value = _line(value)
                if key == "pages":
                    value = re.sub(r"(?<=\w)\s*[-–—]\s*(?=\w)", "--", value)
                fields[key] = _tex(value, literal=key in ("doi", "url"))
        key = "paperbox" + re.sub(r"[^a-zA-Z0-9]", "", paper.id)
        content = f"@{kind}{{{key},\n" + ",\n".join(f"  {key} = {{{value}}}" for key, value in fields.items()) + "\n}\n"
        return content, "application/x-bibtex", "bib"
    if format == "ris":
        fields = [("TY", ris_kind), ("ID", paper.id)]
        fields.extend(("AU", name) for name in authors)
        fields.extend((tag, _line(value)) for tag, value in (("TI", title), ("JO" if ris_kind == "JOUR" else "T2", venue),
            ("PY", year), ("DO", doi), ("VL", paper.volume), ("IS", paper.issue), ("UR", paper.url),
            ("DA", paper.publication_date.isoformat() if paper.publication_date else None)) if value)
        if paper.pages:
            match = re.fullmatch(r"\s*(\d+)\s*[-–—]+\s*(\d+)\s*", paper.pages)
            fields.extend([("SP", match[1]), ("EP", match[2])] if match else [("SP", _line(paper.pages))])
        return "\r\n".join(f"{tag}  - {value}" for tag, value in fields) + "\r\nER  - \r\n", "application/x-research-info-systems", "ris"
    raise ValueError("unsupported citation format")
