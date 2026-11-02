"""Bounded process boundary for MuPDF, which must not run in worker threads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from contextlib import redirect_stdout

_gate_lock = threading.Lock()
_gate = None


def invoke(operation: str, data: bytes) -> dict | list:
    from app.parsing.pdf import PdfParseError

    if os.environ.get("_PAPERBOX_NATIVE_PDF") == "1":
        return _dispatch(operation, data)
    from app.core.config import settings

    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = threading.BoundedSemaphore(settings.pdf_process_concurrency)
    started = time.monotonic()
    if not _gate.acquire(timeout=settings.pdf_process_timeout):
        raise PdfParseError("PDF processor capacity wait exceeded its time limit")
    try:
        result = subprocess.run(
            [sys.executable, "-m", __name__, operation], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(.01, settings.pdf_process_timeout - (time.monotonic() - started)),
            env={**os.environ, "_PAPERBOX_NATIVE_PDF": "1", "OMP_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfParseError("PDF processing exceeded its time limit") from exc
    finally:
        _gate.release()
    if result.returncode:
        raise PdfParseError("PDF processor failed")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PdfParseError("PDF processor returned an invalid result") from exc
    if "error" in payload:
        raise PdfParseError(payload["error"])
    return payload["result"]


def _open(data):
    import pymupdf
    from app.parsing.pdf import PdfParseError

    if not data:
        raise PdfParseError("empty PDF payload")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
        if not document.is_pdf:
            document.close()
            raise PdfParseError("invalid PDF")
        if document.needs_pass and not document.authenticate(""):
            document.close()
            raise PdfParseError("encrypted PDF: password required")
        return document
    except PdfParseError:
        raise
    except Exception as exc:
        raise PdfParseError(f"invalid PDF: {exc}") from exc


def _metadata(document):
    import pymupdf

    info = {}
    kind, value = document.xref_get_key(-1, "Info")
    if kind == "xref":
        index = int(value.split()[0])
        info = {"/" + key: document.xref_get_key(index, key)[1]
                for key in document.xref_get_keys(index)
                if document.xref_get_key(index, key)[0] != "null"}
    elif kind == "dict":
        native = pymupdf.mupdf
        dictionary = native.pdf_dict_gets(native.pdf_trailer(native.pdf_document_from_fz_document(document.this)), "Info")
        for index in range(native.pdf_dict_len(dictionary)):
            key = native.pdf_to_name(native.pdf_dict_get_key(dictionary, index))
            item = document.xref_get_key(-1, "Info/" + key)
            if item[0] != "null":
                info["/" + key] = item[1]
    index = document.xref_xml_metadata()
    return {"info": info, "xmp": document.xref_stream(index).decode("utf-8", errors="replace") if index else ""}


def _lines(page):
    import pymupdf

    content = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    lines = []
    for block_index, block in enumerate(content["blocks"]):
        for line_index, line in enumerate(block.get("lines", [])):
            spans = line["spans"]
            if spans:
                lines.append({"key": [block_index, line_index], "text": "".join(s["text"] for s in spans),
                              "bbox": line["bbox"], "size": max(s["size"] for s in spans),
                              "bold": any(s["flags"] & 16 for s in spans), "dir": line["dir"]})
    return {"width": content["width"], "height": content["height"], "lines": lines}


def _dispatch(operation, data):
    if operation == "parse":
        from app.parsing import pdf, markdown

        return asdict(markdown.render_markdown(pdf.extract_pages(data), pdf_bytes=data, backend="pymupdf"))
    with _open(data) as document:
        if operation == "metadata_bundle":
            return {"metadata": _metadata(document),
                    "pages": [{"page": index + 1, "text": document[index].get_text("text", sort=False)}
                              for index in range(min(2, len(document)))],
                    "lines": _lines(document[0]) if len(document) else {"lines": []}}
        if operation == "metadata":
            return _metadata(document)
        if operation == "pages":
            return [{"page": index + 1, "text": page.get_text("text", sort=False)} for index, page in enumerate(document)]
        if operation == "lines":
            return _lines(document[0]) if len(document) else {"lines": []}
        if operation == "layout":
            return [_lines(page) for page in document]
    raise ValueError("unknown PDF operation")


def main():
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    with redirect_stdout(sys.stderr):
        try:
            payload = {"result": _dispatch(sys.argv[1], sys.stdin.buffer.read())}
        except Exception as exc:
            payload = {"error": str(exc)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
