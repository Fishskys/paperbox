#!/usr/bin/env python3
"""Read-only probe: which endpoints answer without a credential (2026-10-12).

Hits the live API twice per endpoint — once with no credential at all, once with a
bogus one — and prints a matrix. **Nothing here can mutate anything**, on purpose:

* the read probes are the read endpoints themselves (plus the search call);
* every gated probe targets a *nonexistent* paper (all-zero UUID) or runs in
  ``dry_run``, so if a gate were accidentally open the worst case is a 404/422 —
  never an edit, a delete or an import of a real paper.

    uv run python scripts/probe_public_reads.py
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
BOGUS = "pb_bogus_00000000000000000000000000000000"
MISSING = str(uuid.uuid4())
GHOST = "00000000-0000-0000-0000-000000000000"
OUT = Path(__file__).resolve().parents[1] / "logs" / "eval" / "auth-open-20261012"

READS: list[tuple[str, str, dict | None]] = [
    ("GET", "/health", None),
    ("GET", "/api/consistency", None),
    ("GET", "/api/jobs", None),
    ("GET", "/api/jobs/queue", None),
    ("GET", f"/api/jobs/{MISSING}", None),
    ("GET", "/api/metadata/review", None),
    ("GET", "/api/papers", None),
    ("GET", "/api/papers/{paper_id}", None),
    ("GET", "/api/papers/{paper_id}/file", None),
    ("GET", "/api/papers/{paper_id}/chunks", None),
    ("GET", "/api/papers/{paper_id}/degradations", None),
    ("GET", "/api/papers/{paper_id}/metadata", None),
    ("POST", "/api/search", {"query": "attention", "mode": "hybrid", "top_k": 1}),
    ("GET", "/api/search-logs", None),
]

#: Paper-scoped entries use the ghost id, never a real paper.
GATED: list[tuple[str, str, dict | None]] = [
    ("POST", "/api/papers/ingest", {"source": "http://127.0.0.1:9/nothing.pdf"}),
    ("POST", "/api/papers/ingest/files", None),
    ("POST", "/api/papers/ingest/file", None),
    ("POST", "/api/papers/ingest/dir", {"root": "/", "dry_run": True}),
    ("POST", "/api/papers/ingest/compressed", None),
    ("POST", f"/api/jobs/{MISSING}/retry", None),
    ("POST", "/api/papers/reindex", {"dry_run": True}),
    ("POST", "/api/papers/{ghost}/reindex", None),
    ("PATCH", "/api/papers/{ghost}/metadata", {"year": 2024}),
    ("POST", "/api/papers/{ghost}/metadata/rollback", {"field": "year"}),
    ("POST", "/api/papers/{ghost}/metadata/conflicts/dismiss", {"field": "year"}),
    ("POST", "/api/metadata/import", {"dry_run": True}),
    ("POST", f"/api/metadata/sources/{MISSING}/attach", {"paper_id": GHOST}),
    ("POST", "/api/metadata/apply", {"decisions": []}),
    ("DELETE", "/api/papers/{ghost}", None),
]


def fill(path: str, *, paper_id: str = GHOST) -> str:
    return path.replace("{paper_id}", paper_id).replace("{ghost}", GHOST)


def once(client: httpx.Client, method: str, path: str, body: dict | None, headers: dict) -> int:
    return client.request(method, f"{BASE}{path}", json=body, headers=headers).status_code


def main() -> int:
    client = httpx.Client(timeout=180.0)
    listing = client.get(f"{BASE}/api/papers?limit=1")
    papers = (listing.json().get("papers") or []) if listing.status_code == 200 else []
    if not papers:
        print("no live paper to probe paper-scoped routes with", file=sys.stderr)
        return 2
    paper = papers[0]
    paper_id = paper.get("paper_id") or paper.get("id")
    print(f"probe paper: {paper_id}  (anonymous GET /api/papers -> {listing.status_code})\n")

    print("== reads: expected never 401 (anonymous and bogus key alike) ==")
    read_rows, ok_reads = [], 0
    for method, path, body in READS:
        target = fill(path, paper_id=paper_id)
        anon = once(client, method, target, body, {})
        bogus = once(client, method, target, body, {"Authorization": f"Bearer {BOGUS}"})
        good = anon != 401 and bogus != 401
        ok_reads += good
        read_rows.append({"method": method, "path": target, "anon": anon, "bogus": bogus, "open": good})
        print(f"  {'OK ' if good else 'BAD'} {method:6} {target:44} anon={anon} bogus={bogus}")

    print("\n== gated: expected 401 without a credential, 403 with a bogus one ==")
    write_rows, ok_writes = [], 0
    for method, path, body in GATED:
        target = fill(path)
        anon = once(client, method, target, body, {})
        bogus = once(client, method, target, body, {"Authorization": f"Bearer {BOGUS}"})
        good = anon == 401 and bogus == 403
        ok_writes += good
        write_rows.append({"method": method, "path": target, "anon": anon, "bogus": bogus, "gated": good})
        print(f"  {'OK ' if good else 'BAD'} {method:6} {target:44} anon={anon} bogus={bogus}")

    print("\n== /mcp (anonymous) ==")
    mcp_headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def rpc(name: str, params: dict, rid: int) -> httpx.Response:
        return client.post(
            f"{BASE}/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": rid, "method": name, "params": params}),
            headers=mcp_headers,
        )

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "probe", "version": "0"}}, 1)
    listing_rpc = rpc("tools/list", {}, 2)
    tools = [t["name"] for t in (listing_rpc.json().get("result", {}).get("tools") or [])]
    search = rpc("tools/call", {"name": "paper_search",
                                "arguments": {"query": "attention", "top_k": 1}}, 3)
    search_result = search.json().get("result", {})
    search_ok = search.status_code == 200 and not search_result.get("isError")

    print(f"  {'OK ' if init.status_code == 200 else 'BAD'} initialize  -> HTTP {init.status_code}")
    print(f"  {'OK ' if listing_rpc.status_code == 200 else 'BAD'} tools/list  -> HTTP {listing_rpc.status_code}, tools={len(tools)}")
    print(f"  {'OK ' if search_ok else 'BAD'} read tool paper_search -> HTTP {search.status_code}, isError={search_result.get('isError')}")
    print(f"  info  write tools registered: {'paper_reindex' in tools} (MCP_WRITE_ENABLED decides; the anonymous-write FORBIDDEN path is pinned by tests/test_mcp_auth.py)")

    mcp_ok = init.status_code == 200 and listing_rpc.status_code == 200 and search_ok
    print(f"\nsummary: reads open {ok_reads}/{len(READS)}, gated {ok_writes}/{len(GATED)}, mcp anonymous {'ok' if mcp_ok else 'FAILED'}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.json").write_text(
        json.dumps({"base": BASE, "paper_id": paper_id, "reads": read_rows, "gated": write_rows,
                    "mcp": {"initialize": init.status_code, "tools_list": listing_rpc.status_code,
                            "tools": tools, "paper_search": search.status_code, "search_ok": search_ok},
                    "summary": {"reads_open": ok_reads, "reads_total": len(READS),
                                "gated_ok": ok_writes, "gated_total": len(GATED), "mcp_ok": mcp_ok}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"report: {OUT / 'probe.json'}")
    client.close()
    return 0 if ok_reads == len(READS) and ok_writes == len(GATED) and mcp_ok else 1


if __name__ == "__main__":
    sys.exit(main())
