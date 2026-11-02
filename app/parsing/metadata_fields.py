"""Pure PDF field checks shared by extraction, merging and conflict review."""

from __future__ import annotations

from collections.abc import Sequence
import html
import re
import unicodedata

_ROLE = re.compile(r"\b(?:(?:graduate\s+student|senior|life|student)\s+)?(?:member|fellow)(?:\s*,?\s*IEEE)?\b", re.I)
_ORGANIZATION = re.compile(r"\b(?:universit\w*|institut\w*|department|school|college|laborator\w*|labs?|research|corp(?:oration)?|inc|llc|ltd|company|technologies|professor|scientist|IEEE)\b", re.I)
_PUBLICATION = re.compile(r"^.+;\s*(?:19|20)\d{2}\s*;[^;]*;[^;]*(?:;.*)?$", re.S)
_TEMPLATE = re.compile(r"^(?:instructions? for (?:paper|authors?)|paper preparation|session\b|PRIME \d{4} proceedings|(?:NSS-MIC|PPC) \d{4} (?:paper|manuscript)\b|https?://\S+$|www\.\S+$)", re.I)


def text_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(value)).casefold().split())


def abstract_problem(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "empty_abstract"
    text = html.unescape(value).strip()
    if _PUBLICATION.fullmatch(text):
        return "publication_stamp"
    if _TEMPLATE.match(text):
        return "template_or_subject"
    if "\ufffd" in text and text.count("\ufffd") > max(2, len(text) // 50):
        return "damaged_text"
    # A heading is not prose, but DOI mentions, semicolons and short/non-English
    # abstracts are valid. Only reject recognizable publication headings.
    if re.match(r"^(?:(?:19|20)\d{2}\s+)?(?:IEEE\s+)?(?:international\s+)?(?:conference|symposium|workshop)\b", text, re.I) and not re.search(r"[。.!?]\s|\b(?:we|this paper|propose|presents?)\b", text, re.I):
        return "publication_heading"
    return None


def clean_author(value: str) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(value))
    value = re.sub(r"\s+([\u0301\u0308\u030c])([^\W\d_])", lambda m: unicodedata.normalize("NFC", m[2] + m[1]), value)
    value = re.sub(r"^(?:Dr|Prof|Mr|Mrs|Ms)\.\s*", "", value)
    value = _ROLE.sub("", value)
    value = re.sub(r"[0-9*\u2020\u2021\u00a7\u00b6\u2217#]+", "", value)
    return " ".join(value.split()).strip(" ,.;:()")


def parse_authors(value: object) -> list[str]:
    """Decode entities first; preserve inverted names and uncertain comma lists."""
    entries = value if isinstance(value, (list, tuple)) else [value]
    names = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        decoded = html.unescape(entry)
        decoded = _ROLE.sub("", decoded)
        for part in re.split(r"\s*(?:;|\band\b|&|\n)\s*", decoded, flags=re.I):
            comma_parts = [p.strip() for p in part.split(",") if p.strip()]
            # 'Smith, John' is one person; 'Alice Smith, Bob Jones' is two.
            packed = len(comma_parts) > 1 and all(len(p.split()) >= 2 or re.match(r"^(?:[A-Z]\.)+\s*\w", p) for p in comma_parts)
            for name in comma_parts if packed else [part]:
                cleaned = clean_author(name)
                if cleaned:
                    names.append(cleaned)
    return names


def author_problem(name: str, *, title: str | None = None) -> str | None:
    if not name or not any(c.isalpha() for c in name):
        return "empty_author"
    if "@" in name or re.search(r"https?://|www\.", name, re.I):
        return "author_contact"
    if _ROLE.search(name) or _ORGANIZATION.search(name):
        return "author_affiliation_or_role"
    if re.search(r"\b(?:for|using|based|platform|converters?|circuits?|digital|analog|ADC|TDC)\b", name, re.I):
        return "author_title_or_prose"
    if re.search(r"[<>#=\\]|\ufffd", name):
        return "damaged_author"
    words = re.findall(r"[^\W\d_]+", text_key(name))
    if len(words) == 1 and (len(words[0]) < 2 or not any(c.isupper() or c.isalpha() and not c.isascii() for c in name)):
        return "unverified_account_name"
    if title:
        title_words = set(re.findall(r"[^\W\d_]+", text_key(title)))
        if len(words) > 1 and set(words) <= title_words:
            return "author_title_fragment"
    return None


def authors_problem(value: object, *, title: str | None = None) -> str | None:
    if not isinstance(value, (list, tuple)) or not value:
        return "empty_authors"
    return next((problem for name in value if (problem := author_problem(str(name), title=title))), None)


def corroborates(value: str, evidence: str) -> bool:
    """Whitespace/wrapping may differ; do not collapse initials or diacritics."""
    compact = lambda s: "".join(c for c in text_key(s) if c.isalnum())
    key = compact(value)
    return bool(key) and key in compact(evidence)


def document_state(text: str, lines: Sequence[dict] = ()) -> str:
    if not text.strip():
        return "missing_text"
    if abstract_problem(text) == "damaged_text":
        return "damaged_text"
    # Distinct strings occupying almost the same line can be stale/invisible
    # text layers. Do not select a byline automatically from such a page.
    occupied: dict[tuple[int, int, int, int], str] = {}
    for line in lines:
        box = tuple(round(float(n) / 3) for n in line["bbox"])
        text = text_key(line["text"])
        if box in occupied and occupied[box] != text:
            return "overlapping_text"
        occupied[box] = text
    return "ok"
