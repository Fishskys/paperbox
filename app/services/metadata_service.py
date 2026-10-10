"""First-page metadata heuristics for the ingestion pipeline.

Everything here is regex/line based on the already-extracted PDF text layer
(``app.parsing.pdf``) and deliberately conservative: when a field cannot be
found with confidence it stays ``None`` instead of guessing.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from pathlib import Path
from collections.abc import Iterable, Mapping, Sequence

from app.parsing.pdf import EmbeddedMetadata, PageText
from app.services.paper_service import normalize_arxiv_id

_ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9.\-/]+)", re.IGNORECASE)
_ARXIV_LINE = re.compile(
    r"arxiv[:\s]*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Za-z\-]+)?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_AFFILIATION = re.compile(
    r"\b(university|universit|institute|institut|laboratory|lab\b|department|school|college|"
    r"academy|research center|centre|inc\.|corp\.|google|microsoft|meta ai|deepmind)\b",
    re.IGNORECASE,
)
_ABSTRACT_MARKER = re.compile(r"^abstract\b[\s:.\-—]*", re.IGNORECASE)
_SECTION_MARKER = re.compile(
    r"^(?:\d+(?:\.\d+)*|[IVX]+)[.)]?\s+\S|^(introduction|keywords|index terms|1\s+introduction)\b",
    re.IGNORECASE,
)
_AUTHOR_SPLIT = re.compile(r"\s*(?:,|;|\band\b|&)\s*", re.IGNORECASE)
_TITLE_STOPWORDS = re.compile(
    r"^(arxiv|preprint|doi|http|www\.|©|copyright|\d{4}\s|abstract\b|keywords\b)", re.IGNORECASE
)
#: Boilerplate that shows up above the title (license banners, journal lines).
_TITLE_BOILERPLATE = re.compile(
    r"(provided proper attribution|hereby grants|permission to reproduce|"
    r"all rights reserved|under review|submitted to|published as|"
    r"proceedings of|conference on|journal of|transactions on|volume \d|"
    r"workshop on|preprint|arxiv:|doi:|https?://|©|copyright)",
    re.IGNORECASE,
)
_FOOTER = re.compile(r"^(?:\d{1,3}|page \d+|\d+\s*of\s*\d+)$", re.IGNORECASE)
_CAPITALISED_WORD = re.compile(r"^[A-Z\u00c0-\u00de][\w'’\-\.]*$")
_NAME_NOISE = re.compile(r"[*†‡§¶◦●✳∗°\d]+")
#: Lowercase name particles that legitimately appear inside a real name
#: ("Ludwig van Beethoven", "Maria de la Cruz"). Anything else lowercase inside a
#: name-shaped line means the line is prose, not a byline.
_NAME_PARTICLES = frozenset(
    {
        "al", "ben", "bin", "da", "de", "del", "della", "den", "der", "di", "dos",
        "du", "el", "ibn", "la", "le", "mac", "mc", "o", "san", "st", "ten",
        "ter", "van", "vda", "vel", "von", "y",
    }
)
#: Words that never belong to a person's name. Their presence in a candidate line
#: is the cheapest signal that the line is a title, a caption or abstract prose --
#: which is exactly how a mis-parsed page used to smuggle fragments into ``authors``
#: ("Collaborative Platform", "for Social", "we propose an automated").
_NAME_STOPWORDS = frozenset(
    {
        # 注意：不要放单字母词（"a"）。它和名字里的中间名首字母 "A." 撞车，
        # 而小写的 "a" 本来就会被下面"每个词必须首字母大写/是姓名粒子"这一关挡掉。
        "an", "and", "any", "are", "as", "at", "be", "based", "between", "both",
        "but", "by", "can", "design", "do", "does", "during", "each", "efficient",
        "for", "from", "framework", "good", "has", "have", "how", "in", "into",
        "is", "it", "its", "may", "method", "more", "most", "new", "not", "of",
        "on", "or", "our", "out", "over", "paper", "results", "show", "shows",
        "such", "system", "than", "that", "the", "their", "then", "there", "these",
        "they", "this", "those", "through", "to", "toward", "towards", "under",
        "up", "use", "used", "uses", "using", "via", "was", "we", "were", "what",
        "when", "where", "which", "while", "who", "will", "with", "within",
        "without", "would",
        # Nouns that show up in titles, front matter and affiliations but not in a
        # person's name. They are what made capitalised *phrases* pass the shape gate
        # ("Collaborative Platform", "Additional Key Words", "Ant Group",
        # "Graduate Student Member"). Deliberately not a big blocklist: only words
        # that cannot be a given or family name in this corpus.
        "additional", "automation", "collaborative", "edited", "editor", "editorial",
        "graduate", "group", "inc", "institute", "language", "laboratory", "member",
        "peer", "phrase", "phrases", "platform", "research", "review", "science",
        "student", "understanding", "university", "volume", "words",
    }
)
#: Lines that end the author block just as reliably as "Abstract" does. Camera-ready
#: PDFs print ACM/IEEE front matter between the byline and the abstract, and every
#: one of these lines used to be fed to the name splitter.
_AUTHOR_BLOCK_END = re.compile(
    r"^(?:additional\s+key\s+words|key\s+words|keywords|index\s+terms|phrases|"
    r"ccs\s+concepts|acm\s+reference\s+format|abstract)\b[:\s]",
    re.IGNORECASE,
)


def arxiv_id_from_url(url: str | None) -> str | None:
    """Extract an arXiv id from an ``arxiv.org/abs/<id>`` style URL."""
    if not url:
        return None
    match = _ARXIV_IN_URL.search(url)
    if match is None:
        return None
    candidate = match.group(1).strip().strip("/.")
    candidate = re.sub(r"\.pdf$", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"v\d+$", "", candidate, flags=re.IGNORECASE)
    return candidate or None


def arxiv_id_from_text(text: str | None) -> str | None:
    """Extract an arXiv id from a first-page text block, if present."""
    if not text:
        return None
    match = _ARXIV_LINE.search(text)
    if match is None:
        return None
    candidate = match.group(1).strip()
    candidate = re.sub(r"v\d+$", "", candidate, flags=re.IGNORECASE)
    return candidate or None


def _candidate_lines(pages: Sequence[PageText], limit: int) -> list[str]:
    if not pages:
        return []
    lines: list[str] = []
    for page in pages[:limit]:
        for raw in page.text.split("\n"):
            line = raw.strip()
            if line:
                lines.append(line)
    return lines


def _looks_like_title(line: str) -> bool:
    if len(line) < 12 or len(line) > 300:
        return False
    if _TITLE_STOPWORDS.search(line):
        return False
    if _EMAIL.search(line) or _AFFILIATION.search(line):
        return False
    if _FOOTER.match(line):
        return False
    letters = sum(character.isalpha() for character in line)
    return letters >= len(line) * 0.5


def _is_boilerplate(line: str) -> bool:
    if _TITLE_BOILERPLATE.search(line):
        return True
    if _EMAIL.search(line) or _AFFILIATION.search(line):
        return True
    return bool(_FOOTER.match(line))


def _title_candidates(pages: Sequence[PageText], limit: int = 30) -> list[str]:
    """Lines of the first page that could plausibly belong to the title block."""
    lines = _candidate_lines(pages, limit=1)[:limit]
    candidates: list[str] = []
    for line in lines:
        if _is_boilerplate(line):
            continue
        candidates.append(line)
    return candidates


def detect_title(
    pages: Sequence[PageText], sized_lines: Sequence["SizedLine"] | None = None
) -> str | None:
    """Title candidate from the first page.

    Preferred signal is the font size of the first page (``sized_lines``): the
    title is rendered in the largest font, so consecutive largest-font lines are
    joined. Without font information the first plausible line above the author
    block wins - not the longest one, since licence banners are usually wider.
    """
    from app.parsing.pdf import SizedLine  # local import to avoid a cycle

    if sized_lines:
        usable = [
            line
            for line in sized_lines[:40]
            if line.text.strip() and not _is_boilerplate(line.text)
        ]
        if usable:
            biggest = max(line.size for line in usable)
            if biggest > 0:
                threshold = biggest * 0.92
                chosen = [line.text for line in usable if line.size >= threshold]
                if chosen:
                    joined = re.sub(r"\s+", " ", " ".join(chosen)).strip(" .;:")
                    if _looks_like_title(joined):
                        return joined or None

    candidates = _title_candidates(pages)
    fallback: str | None = None
    for index, line in enumerate(candidates[:12]):
        if not _looks_like_title(line):
            continue
        # Skip lines that are obviously a continuation of a previous sentence.
        if line[:1].islower():
            continue
        words = line.split()
        if len(words) < 2:
            continue
        if len(words) <= 4 and not any(len(word) > 3 for word in words):
            continue
        fallback = fallback or line
        # Author lines look like "Jane Doe and John Smith" or "Jane Doe, John Smith".
        if _looks_like_author_line(line):
            continue
        title = line
        nxt = candidates[index + 1] if index + 1 < len(candidates) else None
        if nxt and (line.endswith((",", ":", "-", "of", "and", "the", "for", "with")) or nxt[:1].islower()):
            merged = f"{line} {nxt}"
            if len(merged) <= 300:
                title = merged
        return re.sub(r"\s+", " ", title).strip(" .;:") or None
    if fallback:
        return re.sub(r"\s+", " ", fallback).strip(" .;:") or None
    return None


def _looks_like_author_line(line: str) -> bool:
    """True for "Jane Doe, John Smith" style lines (no sentence structure)."""
    if len(line) > 160 or line.endswith("."):
        return False
    words = line.split()
    if not (2 <= len(words) <= 12):
        return False
    if not all(_CAPITALISED_WORD.match(_NAME_NOISE.sub("", word)) for word in words if len(word) > 1):
        return False
    return bool(re.search(r",|\band\b|&", line)) or len(words) <= 6


def detect_authors(pages: Sequence[PageText], title: str | None = None) -> list[str]:
    """Author names from the block between the title and the abstract.

    Names are taken line by line: e-mail addresses, affiliations and footnotes
    are skipped, comma/``and``-separated lists are split, and a run of bare
    capitalised names on one line (common in camera-ready PDFs) is split into
    two-word names. Two guards keep the page's *other* text out of the byline: the
    title's own fragments are dropped (a wrapped title is otherwise indistinguishable
    from a row of names), and every candidate has to pass :func:`_plausible_author`,
    which rejects lines containing prose words.
    """
    lines = _candidate_lines(pages, limit=1)
    if not lines:
        return []
    title_words = _title_word_set(title)
    start = 0
    if title:
        normalized_title = re.sub(r"\s+", " ", title).strip().casefold()
        for index, line in enumerate(lines[:40]):
            if normalized_title in re.sub(r"\s+", " ", line).strip().casefold():
                start = index + 1
                break
    authors: list[str] = []
    seen: set[str] = set()
    for line in lines[start : start + 40]:
        stripped = line.strip()
        if not stripped:
            continue
        folded = stripped.casefold()
        if (
            _ABSTRACT_MARKER.match(stripped)
            or _AUTHOR_BLOCK_END.match(stripped)
            or _SECTION_MARKER.match(stripped)
        ):
            break
        if _EMAIL.search(stripped) or _is_boilerplate(stripped):
            continue
        if re.search(r"\d{4}", stripped) and "@" in stripped:
            continue
        if _is_title_fragment(stripped, title_words):
            continue
        names = _names_from_line(stripped)
        for name in names:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                authors.append(name)
        if len(authors) >= 20:
            break
    return authors


def _title_word_set(title: str | None) -> frozenset[str]:
    """Content words of the title, for spotting the title's own fragments."""
    if not title:
        return frozenset()
    return frozenset(word for word in re.split(r"\W+", title.casefold()) if len(word) > 1)


def _is_title_fragment(line: str, title_words: frozenset[str]) -> bool:
    """True when every content word of ``line`` also appears in the title.

    A title that wraps over two lines never satisfies the "is the whole title on
    this line" probe in :func:`detect_authors`, so its tail used to reach the name
    splitter -- and the even-word-count branch cheerfully cut it into two-word
    "names": "Collaborative Platform", "for Social", "Science Automation".
    """
    if not title_words:
        return False
    words = [word for word in re.split(r"\W+", line.casefold()) if len(word) > 1]
    if len(words) < 2:
        return False
    return all(word in title_words for word in words)


def _names_from_line(line: str) -> list[str]:
    """Extract plausible author names from one line."""
    if re.search(r",|\band\b|&|;", line):
        parts = [part for part in _AUTHOR_SPLIT.split(line) if part.strip()]
    else:
        words = line.split()
        if 2 <= len(words) <= 4:
            parts = [line]
        elif len(words) % 2 == 0 and 4 < len(words) <= 10:
            # "Kaiming He Xiangyu Zhang Shaoqing Ren Jian Sun" -> pairs.
            parts = [" ".join(words[index : index + 2]) for index in range(0, len(words), 2)]
        else:
            parts = []
    names: list[str] = []
    for part in parts:
        cleaned = _strip_author_noise(part)
        if _plausible_author(cleaned):
            names.append(cleaned)
    return names


def _strip_author_noise(name: str) -> str:
    cleaned = _NAME_NOISE.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
    return cleaned


def _plausible_author(name: str) -> bool:
    """Whether a candidate string looks like a person's name.

    Shape only -- no name list, no language model. Every word must start with a
    capital (or be a known particle such as ``van``/``de``), and a single stopword
    anywhere disqualifies the candidate. That last check is what keeps prose out:
    "for Social", "from Topology" and "we propose an automated" all parse as
    capitalised word pairs, which is why they ended up in ``paper_authors``.
    """
    if not (3 <= len(name) <= 80):
        return False
    if _AFFILIATION.search(name) or _EMAIL.search(name):
        return False
    words = name.split()
    if not (1 < len(words) <= 5):
        return False
    if any(word.casefold().strip(".,;:") in _NAME_STOPWORDS for word in words):
        return False
    for word in words:
        core = word.strip(".,;:'\u2019-")
        if not core:
            return False
        if core.casefold() in _NAME_PARTICLES:
            continue
        # ``str.isupper()`` 而不是 ``_CAPITALISED_WORD``：后者的大写区间只到拉丁-1
        # （U+00DE），会把 "Łukasz Kaiser" 这种名字判成不合法（真机误伤过）。
        if not core[0].isupper():
            return False
    return all(re.search(r"[A-Za-z\u4e00-\u9fff]", word) for word in words)


def detect_abstract(pages: Sequence[PageText], max_pages: int = 2) -> str | None:
    """The ``Abstract`` paragraph, joined across wrapped lines."""
    collected: list[str] = []
    in_abstract = False
    for page in pages[:max_pages]:
        for raw in page.text.split("\n"):
            line = raw.strip()
            if not in_abstract:
                if _ABSTRACT_MARKER.match(line):
                    remainder = _ABSTRACT_MARKER.sub("", line).strip()
                    if remainder:
                        collected.append(remainder)
                    in_abstract = True
                continue
            if not line:
                if collected:
                    return _join_abstract(collected)
                continue
            if _SECTION_MARKER.match(line) or line.casefold().startswith("index terms"):
                return _join_abstract(collected)
            collected.append(line)
    return _join_abstract(collected) if collected else None


def _join_abstract(lines: Iterable[str]) -> str | None:
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return text or None


def _year_from_arxiv_id(arxiv_id: str | None) -> int | None:
    """``1706.03762`` -> 2017 (arXiv ids start with YYMM since 2007)."""
    if not arxiv_id:
        return None
    match = re.match(r"^(\d{2})(\d{2})\.\d{4,5}$", arxiv_id)
    if match is None:
        return None
    year = 2000 + int(match.group(1))
    return year if 2007 <= year <= 2100 else None


def detect_year(pages: Sequence[PageText], url: str | None = None) -> int | None:
    """Publication year.

    The arXiv id is authoritative when the paper came from arXiv; otherwise the
    most frequent plausible year on the first page wins (later pages are full of
    citation years).
    """
    from_id = _year_from_arxiv_id(arxiv_id_from_url(url))
    if from_id is not None:
        return from_id

    counts: Counter[int] = Counter()
    for page in list(pages)[:2]:
        for match in _YEAR.finditer(page.text):
            counts[int(match.group(1))] += 1
    if counts:
        return max(counts.items(), key=lambda item: (item[1], item[0]))[0]
    return None


def detect_doi(pages: Sequence[PageText]) -> str | None:
    """First DOI-looking token on the first page."""
    for page in list(pages)[:1]:
        match = _DOI.search(page.text)
        if match is not None:
            return match.group(0).rstrip(".,;")
    return None


def extract_metadata(
    pages: Sequence[PageText],
    url: str | None = None,
    pdf_bytes: bytes | None = None,
) -> dict[str, object]:
    """Bundle every heuristic above into one metadata dict.

    ``pdf_bytes`` (when given) enables font-size based title detection.
    """
    from app.parsing.pdf import extract_sized_lines

    ordered = list(pages)
    sized_lines = None
    if pdf_bytes:
        try:
            sized_lines = extract_sized_lines(pdf_bytes, 0)
        except Exception:  # noqa: BLE001 - metadata extraction is best effort
            sized_lines = None
    title = detect_title(ordered, sized_lines=sized_lines)
    authors = detect_authors(ordered, title=title)
    result: "OrderedDict[str, object]" = OrderedDict()
    result["title"] = title
    result["abstract"] = detect_abstract(ordered)
    result["year"] = detect_year(ordered, url=url)
    result["authors"] = authors
    arxiv_id = arxiv_id_from_url(url)
    if arxiv_id is None:
        first_page = "\n".join(page.text for page in ordered[:1])
        arxiv_id = arxiv_id_from_text(first_page)
    result["arxiv_id"] = arxiv_id
    result["doi"] = detect_doi(ordered)
    return dict(result)


# --------------------------------------------------------------------------- #
# claim values (what the metadata layer actually stores)
# --------------------------------------------------------------------------- #
def heuristic_claim_values(metadata: Mapping[str, object]) -> dict[str, object]:
    """``extract_metadata`` output -> ``{provenance field: value}``.

    This is the shape the merge engine consumes, so the heuristic path and the
    import path go through exactly the same code.
    """
    values: dict[str, object] = {}
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        values["title"] = title.strip()
    abstract = metadata.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        values["abstract"] = abstract.strip()
    year = metadata.get("year")
    if isinstance(year, int):
        values["year"] = year
    authors = metadata.get("authors") or []
    if authors:
        values["authors"] = [str(name) for name in authors]
    doi = metadata.get("doi")
    if isinstance(doi, str) and doi.strip():
        values["identifier:doi"] = doi.strip()
    arxiv_id = metadata.get("arxiv_id")
    if isinstance(arxiv_id, str) and arxiv_id.strip():
        values["identifier:arxiv"] = arxiv_id.strip()
    return values


#: ``1706.03762`` / ``2105.11453v2`` as they turn up inside a file name. The
#: leading ``YYMM`` must be a real month -- that is what keeps a version-ish
#: tail such as ``notes-2024.12345.pdf`` from being read as an identifier --
#: and the lookarounds stop a match inside a longer digit run.
ARXIV_ID_TOKEN = re.compile(r"(?<![\d.])(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?(?![\d.])")


def arxiv_id_from_filename(filename: str | None) -> str | None:
    """The arXiv id carried by a file name, or ``None``.

    The corpus convention is ``<arxiv_id>__<topic>.pdf`` (see the master plan),
    and dropped-in files are often named the same way. A name is only ever a
    *hint* -- it says which paper a file holds, not what the paper's metadata
    is -- so this feeds the identifier ladder and nothing else.
    """
    if not filename:
        return None
    stem = re.sub(r"\.pdf$", "", Path(filename).name, flags=re.IGNORECASE)
    for match in ARXIV_ID_TOKEN.finditer(stem):
        if 1 <= int(match.group(2)) <= 12:
            return normalize_arxiv_id(match.group(0))
    return None


def filename_claim_values(filename: str | None) -> dict[str, object]:
    """File-name claims -- discovery layer 3, the weakest one.

    Only the identifier: titles, authors and years come from the document
    itself (layers 1 and 2), never from whatever the file happens to be called.
    """
    arxiv_id = arxiv_id_from_filename(filename)
    return {"identifier:arxiv": arxiv_id} if arxiv_id else {}


def embedded_claim_values(embedded: "EmbeddedMetadata") -> dict[str, object]:
    """PDF Info/XMP metadata -> ``{provenance field: value}`` (discovery layer 1).

    Everything here is *structured* as far as rule R2 is concerned: the publisher
    wrote it, so it may correct what the first-page heuristics guessed.
    """
    values: dict[str, object] = {}
    if embedded is None:
        return values
    if embedded.title:
        values["title"] = embedded.title
    if embedded.abstract:
        values["abstract"] = embedded.abstract
    if embedded.authors:
        values["authors"] = list(embedded.authors)
    if embedded.year:
        values["year"] = int(embedded.year)
    if embedded.doi:
        values["identifier:doi"] = embedded.doi
    if embedded.arxiv_id:
        values["identifier:arxiv"] = embedded.arxiv_id
    if embedded.venue:
        venue: dict[str, object] = {"name": embedded.venue}
        if embedded.year:
            venue["year"] = int(embedded.year)
        values["venue"] = venue
    if embedded.volume:
        values["volume"] = embedded.volume
    if embedded.issue:
        values["issue"] = embedded.issue
    if embedded.pages:
        values["pages"] = embedded.pages
    if embedded.publication_date:
        values["publication_date"] = embedded.publication_date
    if embedded.language:
        values["language"] = embedded.language
    if embedded.keywords:
        values["tag:author_terms"] = list(embedded.keywords)
    return values


__all__ = [
    "arxiv_id_from_text",
    "arxiv_id_from_url",
    "detect_abstract",
    "detect_authors",
    "detect_doi",
    "detect_title",
    "detect_year",
    "embedded_claim_values",
    "extract_metadata",
    "heuristic_claim_values",
]
