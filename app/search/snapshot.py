"""The metadata snapshot of a paper, as written into every chunk document.

``POST /api/search`` filters read fields that live *inside* the index, so a
metadata change in PostgreSQL only reaches filtering when the documents are
rewritten. Two writers must agree on what that snapshot contains:

* ``app/workers/tasks._index_rows`` writes it while indexing a paper, and
* ``scripts/refresh_index_metadata.py`` rewrites it for documents that are
  already indexed (no re-embedding, so it costs seconds instead of hours).

Both call :func:`paper_metadata_snapshot`, and the field names/types are declared
in ``app/search/mappings.py`` - that trio has to stay in step, otherwise a filter
silently matches nothing.
"""

from __future__ import annotations

from typing import Any

from app.search.mappings import TAG_KIND_FIELDS
from app.services import paper_service

#: Snapshot keys that are *not* carried by ``paper_chunks`` itself.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "venue_year",
    "paper_type",
    "volume",
    "issue",
    "pages",
    "publication_date",
    "identifiers",
    "ieee_terms",
    "author_terms",
    "dynamic_index_terms",
    "source_tags",
)


def tag_names_by_kind(paper: Any) -> dict[str, list[str]]:
    """``papers_tags`` links grouped into the index fields of ``TAG_KIND_FIELDS``.

    IEEE index terms, author terms, dynamic index terms and source tags answer
    different questions, so the index keeps them in separate keyword fields (the
    flat ``tags`` list stays for backward compatibility). Names are stored
    **normalized** (``metadata_tags.normalize_tag``) so the filters match the
    same keys ``GET /api/papers`` matches on (review 2026-10-05, P1-8).
    """
    from app.services import metadata_tags

    grouped: dict[str, list[str]] = {}
    for link in getattr(paper, "tag_links", []) or []:
        tag = getattr(link, "tag", None)
        if tag is None:
            continue
        kind = str(getattr(link, "kind", "") or "").strip().lower()
        field = TAG_KIND_FIELDS.get(kind)
        if field is None:
            continue
        grouped.setdefault(field, []).append(metadata_tags.normalize_tag(tag.name))
    return {field: sorted(set(names)) for field, names in grouped.items()}


def identifier_strings(paper: Any) -> list[str]:
    """``scheme:normalized_value`` for every ``paper_identifiers`` row.

    One keyword field then answers "which paper has this DOI / arXiv id / IEEE
    article number" without a per-scheme field in the mapping.
    """
    values: set[str] = set()
    for row in getattr(paper, "identifiers", []) or []:
        scheme = str(getattr(row, "scheme", "") or "").strip().lower()
        normalized = getattr(row, "normalized_value", None)
        if scheme and normalized:
            values.add(f"{scheme}:{normalized}")
    return sorted(values)


def _normalized_venue_key(name: str) -> str:
    """The same normalized key ``GET /api/papers`` matches venues on."""
    from app.services.venue_service import normalize_venue_name

    return normalize_venue_name(name)


def paper_metadata_snapshot(paper: Any) -> dict[str, Any]:
    """The filter fields of one paper (see ``app/search/mappings.py``).

    ``venue`` is the venue *normalized key* and ``venue_year`` the year of the
    edition the paper appeared in, so "the conference" and "the conference in a
    given year" are two different filters. ``tags`` is the flat union kept for
    backward compatibility; the per-kind lists are the precise ones. Both venue
    and tag names are stored **normalized** so ``POST /api/search`` matches the
    same keys ``GET /api/papers`` matches on (review 2026-10-05, P1-8) -- facet
    buckets therefore show normalized names too.
    """
    snapshot: dict[str, Any] = {
        "venue": (
            _normalized_venue_key(paper.venue.name) if paper.venue is not None else None
        ),
        "venue_year": paper.venue_year,
        "paper_type": paper.paper_type,
        "volume": paper.volume,
        "issue": paper.issue,
        "pages": paper.pages,
        "publication_date": paper.publication_date,
        "identifiers": identifier_strings(paper),
    }
    by_kind = tag_names_by_kind(paper)
    for field in TAG_KIND_FIELDS.values():
        snapshot[field] = by_kind.get(field, [])
    snapshot["tags"] = sorted({name for names in by_kind.values() for name in names})
    return snapshot


__all__ = [
    "SNAPSHOT_FIELDS",
    "identifier_strings",
    "paper_metadata_snapshot",
    "tag_names_by_kind",
]
