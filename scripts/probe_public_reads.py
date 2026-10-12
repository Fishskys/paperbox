#!/usr/bin/env python3
"""Read-only probe: which endpoints answer without a credential (plan D2/D8, 2026-10-12).

The probe reads the deployment's **two switches** off the live server, then asserts
the matrix that belongs to that state — it never assumes one:

* the master switch, off the write surface: a bare ``POST /api/jobs/<uuid>/retry``
  answering 401 means ``AUTH_ENABLED=true``;
* the read door, off the read surface: ``GET /api/papers`` answering 200 with no
  credential means ``AUTH_READ_ACCESS=public`` (the default), 401 means ``key``.

**Nothing here can mutate anything**, on purpose:

* the read probes are the read endpoints themselves (plus the search call);
* every gated probe targets a *nonexistent* paper (all-zero UUID) or runs in
  ``dry_run``, so if a gate were accidentally open the worst case is a 404/422 —
  never an edit, a delete or an import of a real paper.

    uv run python scripts/probe_public_reads.py [base_url]
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
BOGUS = "pb_bogus_00000000000000000000000000000000"
MISSING = str(uuid.uuid4())
GHOST = "00000000-0000-0000-0000-000000000000"
OUT = Path(__file__).resolve().parents[1] / "logs" / "eval" / "read-door-20261012"

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

#: ``(method, path, body, tier)``. Paper-scoped entries use the ghost id, never a
#: real paper; ``tier`` is the level the plan assigns (D8: only the bulk reindex is
#: ``admin``, deletion is ``write``).
GATED: list[tuple[str, str, dict | None, str]] = [
    ("POST", "/api/papers/ingest", {"source": "http://127.0.0.1:9/nothing.pdf"}, "write"),
    ("POST", "/api/papers/ingest/files", None, "write"),
    ("POST", "/api/papers/ingest/file", None, "write"),
    ("POST", "/api/papers/ingest/dir", {"root": "/", "dry_run": True}, "write"),
    ("POST", "/api/papers/ingest/compressed", None, "write"),
    ("POST", f"/api/jobs/{MISSING}/retry", None, "write"),
    ("POST", "/api/papers/reindex", {"dry_run": True}, "admin"),
    ("POST", "/api/papers/{ghost}/reindex", None, "write"),
    ("PATCH", "/api/papers/{ghost}/metadata", {"year": 2024}, "write"),
    ("POST", "/api/papers/{ghost}/metadata/rollback", {"field": "year"}, "write"),
    ("POST", "/api/papers/{ghost}/metadata/conflicts/dismiss", {"field": "year"}, "write"),
    ("POST", "/api/metadata/import", {"dry_run": True}, "write"),
    ("POST", f"/api/metadata/sources/{MISSING}/attach", {"paper_id": GHOST}, "write"),
    ("POST", "/api/metadata/apply", {"decisions": []}, "write"),
    ("DELETE", "/api/papers/{ghost}", None, "write"),
]


def fill(path: str, *, paper_id: str = GHOST) -> str:
    return path.replace("{paper_id}", paper_id).replace("{ghost}", GHOST)


def once(
    client: httpx.Client, method: str, path: str, body: dict | None, headers: dict
) -> int:
    return client.request(method, f"{BASE}{path}", json=body, headers=headers).status_code


def mcp_rpc(client: httpx.Client, name: str, params: dict, rid: int) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    return client.post(
        f"{BASE}/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": rid, "method": name, "params": params}),
        headers=headers,
    )


def probe_paper_id(client: httpx.Client) -> str | None:
    """One live paper id, fetched anonymously when the door allows it.

    On a closed read door the anonymous listing is a 401, so the operator's own
    ``PAPER_API_KEY`` is used **once**, for the id only: the credential-free probes
    below are the thing under test and stay credential-free.
    """
    listing = client.get(f"{BASE}/api/papers?limit=1")
    if listing.status_code == 200:
        papers = listing.json().get("papers") or []
        if papers:
            return papers[0].get("paper_id") or papers[0].get("id")
        return None
    key = (settings.paper_api_key or "").strip()
    if not key or key == "change-me":
        return None
    keyed = client.get(f"{BASE}/api/papers?limit=1", headers={"Authorization": f"Bearer {key}"})
    if keyed.status_code != 200:
        return None
    papers = keyed.json().get("papers") or []
    if not papers:
        return None
    paper_id = papers[0].get("paper_id") or papers[0].get("id")
    print(f"(closed read door: the probe paper id came from PAPER_API_KEY: {paper_id})")
    return paper_id


def probe_mcp(client: httpx.Client, door_open: bool) -> dict:
    init = mcp_rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
        1,
    )
    listing = mcp_rpc(client, "tools/list", {}, 2)
    tools = [t["name"] for t in (listing.json().get("result", {}).get("tools") or [])]
    search = mcp_rpc(
        client,
        "tools/call",
        {"name": "paper_search", "arguments": {"query": "attention", "top_k": 1}},
        3,
    )
    result = search.json().get("result", {})
    text = str(result)[:200]
    if door_open:
        read_ok = search.status_code == 200 and not result.get("isError")
    else:
        read_ok = "FORBIDDEN" in text and "reads are gated" in text

    print(f"  {'OK ' if init.status_code == 200 else 'BAD'} initialize       -> HTTP {init.status_code}")
    print(
        f"  {'OK ' if listing.status_code == 200 else 'BAD'} tools/list       -> "
        f"HTTP {listing.status_code}, tools={len(tools)}"
    )
    print(
        f"  {'OK ' if read_ok else 'BAD'} read tool        -> HTTP {search.status_code}, "
        f"isError={result.get('isError')} (read door {'open' if door_open else 'closed'})"
    )
    print(
        f"  info  write tools registered: {'paper_delete' in tools} "
        "(MCP_WRITE_ENABLED/MCP_ALLOW_* decide; the anonymous-write FORBIDDEN path is "
        "pinned by tests/test_mcp_auth.py)"
    )
    return {
        "initialize": init.status_code,
        "tools_list": listing.status_code,
        "tools": tools,
        "write_tools_registered": "paper_delete" in tools,
        "paper_search": search.status_code,
        "search_isError": bool(result.get("isError")),
        "read_ok": read_ok,
    }


def main() -> int:
    client = httpx.Client(timeout=180.0)
    listing = client.get(f"{BASE}/api/papers?limit=1")
    read_status = listing.status_code
    door_open = read_status == 200
    probe = client.post(f"{BASE}/api/jobs/{MISSING}/retry")
    auth_on = probe.status_code == 401
    print(
        f"detected: AUTH_ENABLED={'true' if auth_on else 'false'}, "
        f"AUTH_READ_ACCESS={'public' if door_open else 'key'}"
        f"  (anon GET /api/papers -> {read_status}, anon POST retry -> {probe.status_code})\n"
    )

    if read_status not in (200, 401):
        print(f"unexpected read probe status {read_status}; giving up", file=sys.stderr)
        return 2
    paper_id = probe_paper_id(client)
    if paper_id is None:
        print(
            "no live paper to probe paper-scoped routes with: the anonymous listing "
            "did not answer and PAPER_API_KEY did not yield one either",
            file=sys.stderr,
        )
        return 2
    print(f"probe paper: {paper_id}\n")

    expected_open = (
        "never 401 with a credential-free caller (D2: the credential is not consulted); "
        "/health is open in every state"
    )
    expected_closed = "401 without a credential, 403 with an invalid one"
    print(f"== reads: expected {expected_open if door_open else expected_closed} ==")
    read_rows, ok_reads = [], 0
    for method, path, body in READS:
        target = fill(path, paper_id=paper_id)
        anon = once(client, method, target, body, {})
        bogus = once(client, method, target, body, {"Authorization": f"Bearer {BOGUS}"})
        if target == "/health" or door_open or not auth_on:
            # /health is not a read-door endpoint: it is never gated (plan D8), so it
            # stays open in every state.
            good = anon != 401 and bogus != 401
        else:
            good = anon == 401 and bogus == 403
        ok_reads += good
        read_rows.append(
            {"method": method, "path": target, "anon": anon, "bogus": bogus, "ok": good}
        )
        print(f"  {'OK ' if good else 'BAD'} {method:6} {target:44} anon={anon} bogus={bogus}")

    print("\n== gated: expected 401 without a credential, 403 with an invalid one ==")
    write_rows, ok_writes = [], 0
    for method, path, body, tier in GATED:
        target = fill(path)
        anon = once(client, method, target, body, {})
        bogus = once(client, method, target, body, {"Authorization": f"Bearer {BOGUS}"})
        good = (anon, bogus) == (401, 403) if auth_on else (anon, bogus) != (401, 403)
        ok_writes += good
        write_rows.append(
            {
                "method": method,
                "path": target,
                "tier": tier,
                "anon": anon,
                "bogus": bogus,
                "ok": good,
            }
        )
        print(
            f"  {'OK ' if good else 'BAD'} {method:6} {target:44} tier={tier:5} "
            f"anon={anon} bogus={bogus}"
        )

    print("\n== /mcp ==")
    mcp = probe_mcp(client, door_open)
    summary = {
        "reads_open_or_closed_ok": ok_reads,
        "reads_total": len(READS),
        "gated_ok": ok_writes,
        "gated_total": len(GATED),
        "mcp_ok": mcp["read_ok"] and mcp["initialize"] == 200 and mcp["tools_list"] == 200,
    }
    print(
        f"\nsummary: reads {summary['reads_open_or_closed_ok']}/{summary['reads_total']}, "
        f"gated {summary['gated_ok']}/{summary['gated_total']}, "
        f"mcp {'ok' if summary['mcp_ok'] else 'FAILED'}"
    )

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe.json").write_text(
        json.dumps(
            {
                "base": BASE,
                "detected": {"auth_enabled": auth_on, "read_door": "public" if door_open else "key"},
                "paper_id": paper_id,
                "reads": read_rows,
                "gated": write_rows,
                "mcp": mcp,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {OUT / 'probe.json'}")
    client.close()
    return 0 if ok_reads == len(READS) and ok_writes == len(GATED) and summary["mcp_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
