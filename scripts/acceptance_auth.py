#!/usr/bin/env python3
"""Real-machine acceptance for the auth system (plan 2026-10-05 section 10, read door 2026-10-12).

    uv run python scripts/acceptance_auth.py            # run against a live server
    uv run python scripts/acceptance_auth.py --cleanup  # only remove this script's keys

The script detects **two independent switches** instead of one, because since the
read door became a policy they no longer move together:

* the **master switch** (``AUTH_ENABLED``) is read off the *write* surface — a bare
  ``POST /api/jobs/<uuid>/retry`` answering 401 means it is on;
* the **read door** (``AUTH_READ_ACCESS``) is read off the *read* surface —
  ``GET /api/papers`` answering 200 with no credential means it is open (``public``,
  the default), 401 means it is closed (``key``).

With the master switch on it then verifies the four levels over REST (public reads /
read / write / admin), the tier swap of plan D8 (deletion is a ``write`` operation,
the bulk reindex is the only ``admin`` one), revocation, and the MCP half of the same
contract. With the master switch off it verifies anonymous admin access and says so.

Keys used for the run are created **directly in the ``api_keys`` table** with
dedicated prefixes (``aaccread`` / ``aaccwrite`` / ``aaccadmin``) and removed again at
the end — the run touches no paper data (the bulk-reindex probe is a ``dry_run``).

Exit codes: 0 all checks passed, 1 at least one failed, 2 server unreachable.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import ApiKey  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import api_key_service as keys  # noqa: E402

BASE = f"http://127.0.0.1:{settings.paper_api_port}"
PREFIXES = ("aaccread", "aaccwrite", "aaccadmin")

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))


def cleanup_keys() -> int:
    """Remove every key this script may have created (idempotent)."""
    session = SessionLocal()
    try:
        removed = (
            session.execute(
                delete(ApiKey).where(ApiKey.prefix.in_(PREFIXES)).returning(ApiKey.id)
            )
            .scalars()
            .all()
        )
        session.commit()
        return len(list(removed))
    finally:
        session.close()


def make_key(role: str, prefix: str) -> str:
    """Create one acceptance key directly in the table; return the full key."""
    session = SessionLocal()
    try:
        _, full_key = keys.create_key(session, prefix=prefix, role=role)
        session.commit()
        return full_key
    finally:
        session.close()


def mcp_rpc(
    client: httpx.Client, method: str, params: dict | None, token: str | None
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"{BASE}/mcp", content=json.dumps(body), headers=headers)


def tool_names(response: httpx.Response) -> set[str]:
    """Names in a ``tools/list`` response (empty when the call did not answer)."""
    try:
        tools = response.json()["result"]["tools"]
    except Exception:  # noqa: BLE001 - a failed call carries no tool list
        return set()
    return {str(tool.get("name")) for tool in tools}


def one_live_paper_id() -> str | None:
    session = SessionLocal()
    try:
        from app.db.models import Paper

        return session.execute(
            select(Paper.id).where(Paper.deleted_at.is_(None)).limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()


def bearer(full_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {full_key}"}


RETRY_PATH = "/api/jobs/{job_id}/retry"
#: The single ``admin`` endpoint (plan D8): a dry run reads and queues nothing.
BULK_REINDEX_PATH = "/api/papers/reindex"


def detect(client: httpx.Client) -> tuple[bool, bool]:
    """``(master switch on, read door open)`` — measured, not assumed."""
    probe = client.post(f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}")
    auth_on = probe.status_code == 401
    check(
        probe.status_code in (401, 404),
        "master switch detected on the write surface",
        f"anonymous POST {RETRY_PATH} -> {probe.status_code}",
    )
    read_probe = client.get(f"{BASE}/api/papers")
    door_open = read_probe.status_code == 200
    check(
        read_probe.status_code in (200, 401),
        "read door detected on the read surface",
        f"anonymous GET /api/papers -> {read_probe.status_code}",
    )
    print(
        f"  detected: AUTH_ENABLED={'true' if auth_on else 'false'}, "
        f"AUTH_READ_ACCESS={'public' if door_open else 'key'}"
    )
    return auth_on, door_open


def check_read_door(
    client: httpx.Client, door_open: bool, material: dict[str, str]
) -> None:
    """The public reads of the plan's matrix (D2/D4)."""
    anonymous = client.get(f"{BASE}/api/papers")
    invalid = client.get(f"{BASE}/api/papers", headers=bearer("not-a-key"))
    if door_open:
        check(
            anonymous.status_code == 200,
            "an open read door answers without a credential",
            f"status={anonymous.status_code}",
        )
        check(
            invalid.status_code == 200 and "www-authenticate" not in invalid.headers,
            "an invalid credential is ignored on an open read (B2)",
            f"status={invalid.status_code}",
        )
    else:
        check(
            anonymous.status_code == 401 and "www-authenticate" in anonymous.headers,
            "a closed read door is a 401 with a Bearer challenge",
            f"status={anonymous.status_code}",
        )
        check(
            invalid.status_code == 403,
            "an unknown credential is a 403 on a closed read door",
            f"status={invalid.status_code}",
        )
        read_ok = client.get(f"{BASE}/api/papers", headers=bearer(material["read"]))
        check(
            read_ok.status_code == 200,
            "a read key passes the closed read door",
            f"status={read_ok.status_code}",
        )


def check_write_tiers(client: httpx.Client, material: dict[str, str]) -> None:
    """The write and admin tiers — identical whichever way the read door points."""
    missing = client.post(f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}")
    check(
        missing.status_code == 401 and "www-authenticate" in missing.headers,
        "a missing credential on a write is a 401 with a Bearer challenge",
        f"status={missing.status_code}",
    )
    unknown = client.post(
        f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}", headers=bearer("not-a-key")
    )
    check(unknown.status_code == 403, "an unknown credential on a write is a 403")

    read_denied = client.post(
        f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}", headers=bearer(material["read"])
    )
    check(
        read_denied.status_code == 403
        and "insufficient role" in read_denied.json().get("detail", ""),
        "a read key cannot touch a write endpoint (403 insufficient role)",
    )

    random_id = str(uuid.uuid4())
    read_delete = client.delete(
        f"{BASE}/api/papers/{random_id}", headers=bearer(material["read"])
    )
    check(
        read_delete.status_code == 403,
        "a read key cannot delete (403 insufficient role)",
    )
    # Plan D8: deletion dropped to the write tier, so a write key reaches the gate and
    # the answer is the business one (404 for a paper that does not exist).
    write_delete = client.delete(
        f"{BASE}/api/papers/{random_id}", headers=bearer(material["write"])
    )
    check(
        write_delete.status_code == 404,
        "a write key passes the delete gate (404 for a missing paper)",
        f"status={write_delete.status_code}",
    )
    admin_delete = client.delete(
        f"{BASE}/api/papers/{random_id}", headers=bearer(material["admin"])
    )
    check(
        admin_delete.status_code == 404,
        "an admin key passes the delete gate as well",
        f"status={admin_delete.status_code}",
    )

    # Plan D8: the bulk/whole-library reindex is the one admin endpoint.
    write_reindex = client.post(
        f"{BASE}{BULK_REINDEX_PATH}", json={"dry_run": True}, headers=bearer(material["write"])
    )
    check(
        write_reindex.status_code == 403
        and "insufficient role" in write_reindex.json().get("detail", ""),
        "a write key cannot start a bulk reindex (admin only)",
        f"status={write_reindex.status_code}",
    )
    admin_reindex = client.post(
        f"{BASE}{BULK_REINDEX_PATH}", json={"dry_run": True}, headers=bearer(material["admin"])
    )
    plan = admin_reindex.json() if admin_reindex.status_code == 200 else {}
    check(
        admin_reindex.status_code == 200
        and plan.get("dry_run") is True
        and plan.get("queued") in (0, None),
        "an admin key gets a bulk-reindex dry run (nothing queued)",
        f"status={admin_reindex.status_code} body={str(plan)[:80]}",
    )


def check_mcp(client: httpx.Client, door_open: bool, material: dict[str, str]) -> None:
    """The MCP half: reads follow the door, writes never do."""
    init_params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "acceptance", "version": "0"},
    }
    handshake = mcp_rpc(client, "initialize", init_params, material["admin"])
    if handshake.status_code == 404:
        print("[SKIP] /mcp not mounted (MCP_ENABLED=false)")
        return
    check(
        handshake.status_code == 200,
        "/mcp accepts a database key (G4)",
        f"status={handshake.status_code}",
    )
    anonymous = mcp_rpc(client, "initialize", init_params, None)
    check(
        anonymous.status_code == 200,
        "/mcp handshake answers without a credential (the transport never refuses)",
        f"status={anonymous.status_code}",
    )
    names = tool_names(mcp_rpc(client, "tools/list", {}, None))
    read_call = mcp_rpc(
        client,
        "tools/call",
        {"name": "paper_search", "arguments": {"query": "attention", "top_k": 1}},
        None,
    )
    if door_open:
        check(
            "result" in read_call.text and "FORBIDDEN" not in read_call.text,
            "an open read door lets a read tool answer without a credential",
            read_call.text[:120],
        )
    else:
        check(
            "FORBIDDEN" in read_call.text and "reads are gated" in read_call.text,
            "a closed read door fails a read tool with FORBIDDEN",
            read_call.text[:120],
        )
        read_key_call = mcp_rpc(
            client,
            "tools/call",
            {"name": "paper_search", "arguments": {"query": "attention", "top_k": 1}},
            material["read"],
        )
        check(
            "FORBIDDEN" not in read_key_call.text,
            "a read key passes the closed read door on the MCP surface",
            read_key_call.text[:120],
        )
    if "paper_delete" in names:
        refusal = mcp_rpc(
            client,
            "tools/call",
            {"name": "paper_delete", "arguments": {"paper_id": str(uuid.uuid4())}},
            material["read"],
        )
        check(
            "FORBIDDEN" in refusal.text,
            "a read key cannot reach a write tool (FORBIDDEN)",
            refusal.text[:120],
        )
    else:
        print("[SKIP] MCP write tools are not registered (MCP_WRITE_ENABLED/MCP_ALLOW_*)")


def run() -> None:
    with httpx.Client(timeout=180) as client:
        # ---- always: health stays credential-free ---- #
        response = client.get(f"{BASE}/health")
        check(response.status_code == 200, "/health answers without a credential")

        auth_on, door_open = detect(client)

        material: dict[str, str] = {}
        if auth_on:
            material = {
                "read": make_key("read", "aaccread"),
                "write": make_key("write", "aaccwrite"),
                "admin": make_key("admin", "aaccadmin"),
            }
            check_read_door(client, door_open, material)
            check_write_tiers(client, material)
            check_mcp(client, door_open, material)

            # ---- revocation bites immediately ---- #
            session = SessionLocal()
            try:
                keys.revoke_key(session, "aaccread")
                session.commit()
            finally:
                session.close()
            # A deleted key is an unknown credential: observable where a credential is
            # read at all — a gated endpoint, or a closed read door.
            revoked = client.post(
                f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}",
                headers=bearer(material["read"]),
            )
            check(revoked.status_code == 403, "a deleted key stops authenticating")
            if not door_open:
                revoked_read = client.get(
                    f"{BASE}/api/papers", headers=bearer(material["read"])
                )
                check(
                    revoked_read.status_code == 403,
                    "a deleted key stops reading on a closed read door",
                    f"status={revoked_read.status_code}",
                )
        else:
            retry = client.post(f"{BASE}{RETRY_PATH.format(job_id=uuid.uuid4())}")
            check(
                retry.status_code == 404,
                "anonymous admin access while AUTH_ENABLED=false",
                f"status={retry.status_code}",
            )
            print(
                "  note: tier enforcement needs AUTH_ENABLED=true — restart with it\n"
                "        on and re-run; startup guards are unit-tested "
                "(tests/test_mcp_auth.py)."
            )
            check_mcp(client, door_open, {"admin": ""})

        # ---- the file route follows the read door ---- #
        paper_id = one_live_paper_id()
        if paper_id is None:
            print("[SKIP] file download: no live paper in the database")
        else:
            download = client.get(f"{BASE}/api/papers/{paper_id}/file")
            if door_open or not auth_on:
                check(
                    download.status_code in (200, 206),
                    "the file route answers without a credential (open read door)",
                    f"status={download.status_code}",
                )
            else:
                check(
                    download.status_code == 401,
                    "the file route needs a credential (closed read door)",
                    f"status={download.status_code}",
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true", help="only remove this script's keys")
    args = parser.parse_args()

    if args.cleanup:
        removed = cleanup_keys()
        print(f"removed {removed} acceptance key(s)")
        return 0

    try:
        run()
    except httpx.ConnectError:
        print(f"server unreachable at {BASE} — start it first", file=sys.stderr)
        return 2
    finally:
        removed = cleanup_keys()
        print(f"cleanup: removed {removed} acceptance key(s)")

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
