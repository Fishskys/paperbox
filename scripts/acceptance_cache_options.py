"""Accept (real machine) that a parse-option change invalidates the parse cache.

2026-09-30. The parse cache used to identify an artifact by (paper, backend,
docling version) only -- so flipping ``DOCLING_FORMULA_ENRICHMENT`` replayed a
markdown that was produced under the *other* setting. This script shows the new
rule end to end against real MinIO + the real docling service:

    formula=False  cold  -> real parse, meta.options.formula == False
    formula=False  warm  -> replay (cache_hit, ~ms)
    formula=True         -> no replay, real parse, markdown now has $$...$$
    formula=True   warm  -> replay again
    formula=False        -> no replay again (the stored artifact is the other one)

Self-cleaning: artifacts live under ``papers/<throwaway id>/`` and are deleted
here; nothing in PostgreSQL / OpenSearch / the live papers is touched. Run with::

    PYTHONPATH=. uv run python scripts/acceptance_cache_options.py
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from app.core.config import settings
from app.services import object_storage, parser_service

PDF = Path("logs/eval/docling/corpus/1807.11311.pdf")
META = "parse-meta.json"


def _meta(paper_id: str, backend: str) -> dict:
    # the meta lives next to the per-backend dirs, not inside them (shared per paper)
    key = f"papers/{paper_id}/extracted/parsed/{META}"
    try:
        raw = object_storage.download_bytes(key)
    except Exception as exc:  # noqa: BLE001 - probe
        return {"_error": f"{type(exc).__name__}: {exc}"}
    return json.loads(raw.decode("utf-8"))


def _objects(paper_id: str) -> list[str]:
    return sorted(str(o.object_name) for o in object_storage.list_objects(prefix=f"papers/{paper_id}/"))


def main() -> int:
    data = PDF.read_bytes()
    paper_id = str(uuid.uuid4())
    store = parser_service._default_store()
    print(f"pdf={PDF.name} {len(data)} bytes  paper_id={paper_id} (throwaway)")
    print(f"backend={settings.parser_backend} docling={settings.docling_url}\n")

    rows: list[tuple[str, object, float, bool, int, object]] = []

    def step(label: str, *, formula: bool, expect_hit: bool | None = None) -> None:
        settings.docling_formula_enrichment = formula
        started = time.perf_counter()
        bundle = parser_service.parse_paper_file(
            paper_id, data, filename=PDF.name, store=store
        )
        seconds = time.perf_counter() - started
        meta = _meta(paper_id, bundle.backend)
        formulas = bundle.markdown.count("$$")
        rows.append((label, bundle.cache_hit, seconds, expect_hit, formulas, meta.get("options")))
        flag = "" if expect_hit is None or bundle.cache_hit is expect_hit else "  <-- UNEXPECTED"
        print(
            f"{label:22} cache_hit={str(bundle.cache_hit):5} {seconds:7.2f}s "
            f"$$x{formulas:<4} options.formula={meta.get('options', {}).get('formula')}{flag}"
        )

    step("formula=False cold", formula=False, expect_hit=False)
    step("formula=False warm", formula=False, expect_hit=True)
    step("formula=True", formula=True, expect_hit=False)
    step("formula=True warm", formula=True, expect_hit=True)
    step("formula=False again", formula=False, expect_hit=False)

    print("\nobjects written:", _objects(paper_id))
    removed = object_storage.delete_prefix(paper_id)
    left = _objects(paper_id)
    print(f"cleanup: removed={removed} left={left}")

    bad = [r for r in rows if r[3] is not None and r[1] is not r[3]]
    meta_ok = all(
        (r[5] or {}).get("formula") is (r[0].startswith("formula=True")) for r in rows
    )
    print(f"\nverdict: cache_hit 判定 {'OK' if not bad else f'{len(bad)} 处不符'}"
          f" / meta.options.formula 记录 {'OK' if meta_ok else 'MISMATCH'}"
          f" / 残留 {'none' if not left else left}")
    return 0 if (not bad and meta_ok and not left) else 1


if __name__ == "__main__":
    raise SystemExit(main())
