"""Text hygiene shared by both parser backends.

Control characters arrive inside the PDF's own text layer: some IEEE-stamped
PDFs encode a handful of glyphs (``Δ``, ``/``, digits inside an equation font)
as NUL, which docling hands through into the markdown it returns -- so every
chunk cut from that markdown carried a ``0x00`` into
``INSERT INTO paper_chunks``, where PostgreSQL refused the whole job with
``DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes``.

The policy lives here, in one place, because **both** backends must apply it:
the pypdf side scrubs each page's text (``app.parsing.pdf.normalize_page_text``)
and its embedded metadata, the docling side scrubs the markdown it is handed
before the dialect normalizes it (``app.parsing.markdown.normalize_markdown``).

* NUL (0x00) can never be stored in a PostgreSQL ``text`` column.
* The remaining C0 controls (and DEL) would be stored, but they corrupt the
  JSON documents sent to OpenSearch.

Tab and newline survive: line structure is what section detection works on.
"""

from __future__ import annotations

#: Control characters removed from extracted text: every C0 code except tab
#: (0x09) and newline (0x0A), plus DEL (0x7F).
CONTROL_CHAR_TABLE = {code: None for code in range(0x20) if code not in (0x09, 0x0A)}
CONTROL_CHAR_TABLE[0x7F] = None


def strip_control_chars(text: str) -> str:
    """Return ``text`` without control characters (tab and newline excepted)."""
    return text.translate(CONTROL_CHAR_TABLE)
