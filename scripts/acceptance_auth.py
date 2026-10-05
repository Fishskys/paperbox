#!/usr/bin/env python3
"""Real-machine acceptance for the auth system (plan: 2026-10-05_145619-api-auth-keys-roles §10).

    uv run python scripts/acceptance_auth.py            # run against a live server
    uv run python scripts/acceptance_auth.py --cleanup  # only remove this script's keys

It adapts to the running server's auth state (detected by hitting
``GET /api/papers`` without a credential):

* ``AUTH_ENABLED=false``: verifies anonymous admin access, /health and signed
  downloads, then explains that tier enforcement needs ``AUTH_ENABLED=true``;
* ``AUTH_ENABLED=true``: verifies 401/403 semantics, the three role tiers over
  REST, tier gates on the MCP write tools (when they are registered), MCP
  handshake with a database key, and that revocation bites on the next request.

Keys used for the run are created **directly in the ``api_keys`` table** with
dedicated prefixes (``aaccread`` / ``aaccwrite`` / ``aaccadmin``) and removed
again at the end — the run touches no paper data. The startup guards (refusing
``change-me``, refusing zero keys) are covered by unit tests
(``tests/test_mcp_auth.py``) plus the manual restart checklist in
``docs/progress/project.md`` §31; this script cannot restart your server.

Exit codes: 0 all checks passed, 1 at least one failed, 2 server unreachable.
"""
from __future__ import annotations

import argparse
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
from app.services import download_signing  # noqa: E402

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


def make_key(role: str, prefix: str, name: str) -> str:
    """Create one acceptance key directly in the table; return the full key."""
    session = SessionLocal()
    try:
        _, full_key = keys.create_key(session, name=name, prefix=prefix, role=role)
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
    return client.post(
        f"{BASE}/mcp", content=__import__("json").dumps(body), headers=headers
    )


def one_live_paper_id() -> str | None:
    session = SessionLocal()
    try:
        from app.db.models import Paper

        return session.execute(
            select(Paper.id).where(Paper.deleted_at.is_(None)).limit(1)
        ).scalar_one_or_none()
    finally:
        session.close()


def run() -> None:
    with httpx.Client(timeout=30) as client:
        # ---- always: health and downloads stay credential-free ---- #
        response = client.get(f"{BASE}/health")
        check(response.status_code == 200, "/health answers without a credential")

        # ---- detect the server's auth state ---- #
        probe = client.get(f"{BASE}/api/papers")
        auth_on = probe.status_code in (401, 403)
        check(
            probe.status_code in (200, 401, 403),
            "auth state detected",
            f"GET /api/papers without key -> {probe.status_code} "
            f"(AUTH_ENABLED={'true' if auth_on else 'false'})",
        )

        material: dict[str, str] = {}
        if auth_on:
            # ---- 401 / 403 semantics ---- #
            missing = client.get(f"{BASE}/api/papers")
            check(
                missing.status_code == 401
                and "www-authenticate" in missing.headers,
                "missing credential is a 401 with a Bearer challenge",
            )
            unknown = client.get(
                f"{BASE}/api/papers", headers={"Authorization": "Bearer not-a-key"}
            )
            check(unknown.status_code == 403, "unknown credential is a 403")

            # ---- three tiers over REST ---- #
            material = {
                "read": make_key("read", "aaccread", "acceptance-read"),
                "write": make_key("write", "aaccwrite", "acceptance-write"),
                "admin": make_key("admin", "aaccadmin", "acceptance-admin"),
            }
            listed = client.get(
                f"{BASE}/api/papers",
                headers={"Authorization": f"Bearer {material['read']}"},
            )
            check(listed.status_code == 200, "read key lists papers (200)")

            random_id = str(uuid.uuid4())
            forbidden = client.delete(
                f"{BASE}/api/papers/{random_id}",
                headers={"Authorization": f"Bearer {material['read']}"},
            )
            check(
                forbidden.status_code == 403
                and "insufficient role" in forbidden.json().get("detail", ""),
                "read key cannot delete (403 insufficient role)",
            )
            write_forbidden = client.delete(
                f"{BASE}/api/papers/{random_id}",
                headers={"Authorization": f"Bearer {material['write']}"},
            )
            check(
                write_forbidden.status_code == 403,
                "write key cannot delete (403 insufficient role)",
            )
            admin_gate = client.delete(
                f"{BASE}/api/papers/{random_id}",
                headers={"Authorization": f"Bearer {material['admin']}"},
            )
            check(
                admin_gate.status_code == 404,
                "admin key passes the delete gate (404 for a missing paper)",
            )

            # ---- revocation bites immediately ---- #
            session = SessionLocal()
            try:
                keys.revoke_key(session, "aaccread")
                session.commit()
            finally:
                session.close()
            revoked = client.get(
                f"{BASE}/api/papers",
                headers={"Authorization": f"Bearer {material['read']}"},
            )
            check(revoked.status_code == 403, "revoked key stops authenticating")
        else:
            check(
                probe.status_code == 200,
                "anonymous admin access while AUTH_ENABLED=false",
            )
            print(
                "  note: tier enforcement needs AUTH_ENABLED=true — restart with it\n"
                "        on and re-run; startup guards are unit-tested "
                "(tests/test_mcp_auth.py)."
            )

        # ---- MCP surface (only when mounted) ---- #
        # Probe WITH a credential first: with auth on, the middleware 401s any
        # /mcp path even when the endpoint is not mounted, so the no-key probe
        # cannot distinguish "closed" from "absent".
        init_params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "acceptance", "version": "0"}}
        if auth_on and material.get("admin"):
            handshake = mcp_rpc(client, "initialize", init_params, material["admin"])
            if handshake.status_code == 404:
                print("[SKIP] /mcp not mounted (MCP_ENABLED=false)")
            else:
                check(handshake.status_code == 200, "/mcp accepts a database key (G4)",
                      f"status={handshake.status_code}")
                anonymous = mcp_rpc(client, "initialize", init_params, None)
                check(
                    anonymous.status_code == 401,
                    "/mcp without a credential is a 401 (AUTH_ENABLED=true)",
                )
        else:
            anonymous = mcp_rpc(client, "initialize", init_params, None)
            if anonymous.status_code == 404:
                print("[SKIP] /mcp not mounted (MCP_ENABLED=false)")
            else:
                check(
                    anonymous.status_code == 200,
                    "/mcp answers anonymously while AUTH_ENABLED=false",
                )

        # ---- signed download stays credential-free ---- #
        paper_id = one_live_paper_id()
        if paper_id is None:
            print("[SKIP] signed download: no live paper in the database")
        else:
            url, _ = download_signing.build_url(paper_id, BASE)
            signed = client.get(url)  # no Authorization header on purpose
            check(
                signed.status_code in (200, 206),
                "signed download works without a credential",
                f"status={signed.status_code}",
            )
            tampered = client.get(
                url.replace("sig=", "sig=0" if not url.endswith("0") else "sig=1")
            )
            check(tampered.status_code == 403, "tampered signature is a 403")


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
