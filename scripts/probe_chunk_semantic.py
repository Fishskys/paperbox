"""A/B the chunk boundary policies on real papers (plan section 2 T7.2).

Length policy vs semantic policy (``CHUNK_MODE=semantic``), same pages, same
sections, same target/hard cap -- only the boundary decision differs. Reports
chunk counts, token distribution, page coverage, wall time and how many
sentences had to be embedded, plus two invariants that must hold in both modes:

* no chunk exceeds ``MAX_TOKENS`` (the 512-token embedding limit stays safe), and
* every source sentence survives into some chunk.

This is a *reader*: it needs the embedding server (semantic mode) but no DB, no
MinIO and no docling. It writes markdown + JSON under ``logs/eval/chunking/``.

Usage::

    uv run python scripts/probe_chunk_semantic.py
    uv run python scripts/probe_chunk_semantic.py --papers logs/eval/docling/corpus/*.pdf
    uv run python scripts/probe_chunk_semantic.py --threshold 0.75 --label t075
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python scripts/probe_*.py`
    sys.path.insert(0, str(ROOT))

from app.parsing.chunking import (  # noqa: E402
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    MAX_TOKENS,
    SEMANTIC_MIN_TOKENS,
    SEMANTIC_SIMILARITY_THRESHOLD,
    Chunk,
    chunk_document,
    split_sentences,
)
from app.parsing.pdf import extract_pages  # noqa: E402
from app.parsing.structure import detect_sections, merge_short_sections  # noqa: E402
from app.services import embedding_service  # noqa: E402

DEFAULT_PAPERS = "logs/eval/docling/corpus/*.pdf"
DEFAULT_OUT = Path("logs/eval/chunking")


class CountingEmbedder:
    """``embed_texts`` plus the numbers the report needs.

    Sentence vectors are cached under ``logs/eval/chunking/.embed-cache`` keyed
    by the sentence list, so re-running with a different threshold (the point of
    this harness) costs no embedding at all. Set ``use_cache=False`` to force a
    fresh call.
    """

    def __init__(self, cache_dir: Path | None = None, *, use_cache: bool = True) -> None:
        self.calls = 0
        self.texts = 0
        self.seconds = 0.0
        self.cached_calls = 0
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, texts: list[str]) -> Path | None:
        if not self.use_cache or self.cache_dir is None:
            return None
        digest = hashlib.sha256("\u0000".join(texts).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest[:32]}.json"

    def __call__(self, texts):
        items = list(texts)
        path = self._cache_path(items)
        if path is not None and path.exists():
            self.cached_calls += 1
            return json.loads(path.read_text(encoding="utf-8"))
        started = time.perf_counter()
        vectors = embedding_service.embed_texts(items)
        self.seconds += time.perf_counter() - started
        self.calls += 1
        self.texts += len(items)
        if path is not None:
            path.write_text(json.dumps(vectors), encoding="utf-8")
        return vectors


def _percentile(values: list[int], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return float(ordered[index])


def _stats(chunks: list[Chunk]) -> dict:
    tokens = [chunk.token_count for chunk in chunks]
    pages = [chunk.page_start for chunk in chunks] + [chunk.page_end for chunk in chunks]
    return {
        "chunks": len(chunks),
        "chars": sum(chunk.char_count for chunk in chunks),
        "tokens_min": min(tokens) if tokens else 0,
        "tokens_median": statistics.median(tokens) if tokens else 0,
        "tokens_p90": _percentile(tokens, 0.9),
        "tokens_max": max(tokens) if tokens else 0,
        "chunks_over_target": sum(1 for value in tokens if value > DEFAULT_TARGET_TOKENS),
        "chunks_over_cap": sum(1 for value in tokens if value > MAX_TOKENS),
        "pages_touched": len(set(pages)),
        "overlap_chunks": sum(1 for chunk in chunks if chunk.is_overlap),
    }


def _is_subsequence(needle: str, haystack: str) -> bool:
    """True when ``needle`` can be found in ``haystack`` in order.

    Deletions and reordering fail; insertions pass -- which is what the overlap
    window does at a chunk boundary, and it is not content loss.
    """
    iterator = iter(haystack)
    return all(char in iterator for char in needle)


def _content_ok(sections, chunks: list[Chunk]) -> tuple[bool, int, int]:
    """``(chars_preserved, sentences_split, sentences)``.

    Two independent checks, both whitespace-insensitive (both policies may cut
    and rejoin with separators, so whitespace is not content):

    * *chars preserved* -- the source text must survive, in order, inside the
      concatenated chunk text. Overlap duplicates text at boundaries, so this is
      a subsequence test rather than substring/equality.
    * *sentences split* -- how many source sentences no single chunk contains
      whole. The length policy splits mid-sentence whenever a paragraph exceeds
      its character window; the semantic policy only at sentence boundaries.
    """
    flat = "".join("".join(chunk.text.split()) for chunk in chunks)
    raw_source = " ".join(
        " ".join(text for _, text in section.paragraphs) for section in sections
    )
    source = " ".join(raw_source.split())
    sentences = split_sentences(source)
    split_count = sum(
        1 for sentence in sentences if "".join(sentence.split()) not in flat
    )
    return _is_subsequence("".join(source.split()), flat), split_count, len(sentences)


def _run(data: bytes, *, embed_fn=None, threshold: float) -> tuple[list[Chunk], list, float]:
    pages = extract_pages(data)
    sections = merge_short_sections(detect_sections(pages))
    started = time.perf_counter()
    chunks = chunk_document(
        pages,
        sections,
        DEFAULT_TARGET_TOKENS,
        DEFAULT_OVERLAP_TOKENS,
        embed_fn=embed_fn,
        semantic_threshold=threshold,
    )
    return chunks, sections, time.perf_counter() - started


def _write_chunks(path: Path, chunks: list[Chunk]) -> None:
    blocks = [
        f"<!-- chunk {chunk.chunk_index}: pages {chunk.page_start}-{chunk.page_end}, "
        f"section={chunk.section!r}, tokens={chunk.token_count} -->\n\n{chunk.text}"
        for chunk in chunks
    ]
    path.write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", default=DEFAULT_PAPERS, help="glob of PDFs")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--label", default="", help="suffix for the output files")
    parser.add_argument(
        "--threshold", type=float, default=SEMANTIC_SIMILARITY_THRESHOLD, help="dip threshold"
    )
    parser.add_argument(
        "--min-tokens", type=int, default=SEMANTIC_MIN_TOKENS, help="semantic chunk floor"
    )
    parser.add_argument("--limit", type=int, default=0, help="max papers (0 = all)")
    args = parser.parse_args()

    paths = sorted(Path(item) for item in glob.glob(args.papers))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"no PDF matched {args.papers!r}")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{args.label}" if args.label else ""
    report: dict = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "threshold": args.threshold,
        "min_tokens": args.min_tokens,
        "target_tokens": DEFAULT_TARGET_TOKENS,
        "max_tokens": MAX_TOKENS,
        "papers": [],
    }

    for path in paths:
        data = path.read_bytes()
        name = path.stem
        length_chunks, sections, length_s = _run(data, threshold=args.threshold)
        length_ok, length_split, length_sents = _content_ok(sections, length_chunks)

        embedder = CountingEmbedder(out_dir / ".embed-cache")
        started = time.perf_counter()
        semantic_chunks, _, semantic_s = _run(
            data, embed_fn=embedder, threshold=args.threshold
        )
        embed_s = embedder.seconds
        semantic_ok, semantic_split, semantic_sents = _content_ok(
            sections, semantic_chunks
        )

        entry = {
            "file": str(path),
            "sections": len(sections),
            "pages": len({page for section in sections for page, _ in section.paragraphs}),
            "length": {
                **_stats(length_chunks),
                "chunk_s": round(length_s, 4),
                "content_ok": length_ok,
                "sentences": length_sents,
                "sentences_split": length_split,
            },
            "semantic": {
                **_stats(semantic_chunks),
                "chunk_s": round(semantic_s, 4),
                "embed_calls": embedder.calls,
                "embed_cache_hits": embedder.cached_calls,
                "embed_texts": embedder.texts,
                "embed_s": round(embed_s, 4),
                "content_ok": semantic_ok,
                "sentences": semantic_sents,
                "sentences_split": semantic_split,
            },
        }
        report["papers"].append(entry)

        _write_chunks(out_dir / f"{name}-length{suffix}.md", length_chunks)
        _write_chunks(out_dir / f"{name}-semantic{suffix}.md", semantic_chunks)

        print(
            f"{name}: sections={entry['sections']} "
            f"length={entry['length']['chunks']} chunks "
            f"(median {entry['length']['tokens_median']} tok, {length_s:.2f}s, "
            f"split {length_split}/{length_sents} sentences, chars_ok={length_ok}) | "
            f"semantic={entry['semantic']['chunks']} chunks "
            f"(median {entry['semantic']['tokens_median']} tok, {semantic_s:.2f}s, "
            f"+{embed_s:.2f}s embedding of {embedder.texts} sentences, "
            f"split {semantic_split}/{semantic_sents} sentences, chars_ok={semantic_ok})"
        )

    (out_dir / f"summary{suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# Chunking A/B — {report['generated_at']}",
        "",
        f"threshold={args.threshold} min_tokens={args.min_tokens} "
        f"target={DEFAULT_TARGET_TOKENS} cap={MAX_TOKENS}",
        "",
        "| paper | sections | length chunks | len median tok | len max tok |"
        " len chars ok | len split sents | semantic chunks | sem median tok | sem max tok |"
        " sem chars ok | sem split sents | embed s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in report["papers"]:
        length = entry["length"]
        semantic = entry["semantic"]
        lines.append(
            f"| {Path(entry['file']).name} | {entry['sections']} "
            f"| {length['chunks']} | {length['tokens_median']} | {length['tokens_max']} "
            f"| {length['content_ok']} | {length['sentences_split']}/{length['sentences']} "
            f"| {semantic['chunks']} | {semantic['tokens_median']} | {semantic['tokens_max']} "
            f"| {semantic['content_ok']} "
            f"| {semantic['sentences_split']}/{semantic['sentences']} "
            f"| {semantic['embed_s']} |"
        )
    over_cap = sum(
        entry[mode]["chunks_over_cap"]
        for entry in report["papers"]
        for mode in ("length", "semantic")
    )
    lines.append("")
    lines.append(
        "- *chars ok* = the source text survives, in order, in the concatenated chunk "
        "text (subsequence test; the overlap window's duplicated text is an insertion, "
        "not loss)."
    )
    lines.append(
        "- *split sents* = source sentences no single chunk contains whole "
        "(the length policy cuts mid-sentence inside an oversized paragraph)."
    )
    lines.append(f"- chunks over the {MAX_TOKENS}-token cap, all papers: {over_cap}.")
    lines.append(
        f"- `embed s` is 0 on a re-run: sentence vectors are cached in "
        f"`.embed-cache/` keyed by the sentence list, so only the first run pays."
    )
    lines.append(
        f"Raw: `summary{suffix}.json`; per-chunk dumps: `*-length{suffix}.md` / "
        f"`*-semantic{suffix}.md`."
    )
    (out_dir / f"summary{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {out_dir}/summary{suffix}.json (+ .md, + per-chunk dumps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
