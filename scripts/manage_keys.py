#!/usr/bin/env python3
"""Create, list, or revoke API keys (plan: 2026-10-05_145619-api-auth-keys-roles).

    uv run python scripts/manage_keys.py create --name hermes --prefix hermes --role admin
    uv run python scripts/manage_keys.py list
    uv run python scripts/manage_keys.py revoke hermes --yes

A created key is printed **once** and stored only as a sha256 hash: losing the
printed secret means revoking and re-issuing, there is no recovery. Keys come
in three monotonic tiers — ``read`` < ``write`` < ``admin`` — enforced on both
the REST and the MCP surface (``AUTH_ENABLED=true``). Rows derived from the
environment (``PAPER_API_KEY`` / ``PAPER_API_KEYS`` bootstrap admins) show up in
``list`` with ``source=env`` and refuse ``revoke``: remove them from the env
file and restart instead.

Exit codes: 0 success, 1 refused (bad input, clash, unknown prefix),
2 the database is unreachable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.services import api_key_service as keys  # noqa: E402


def cmd_create(args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        row, full_key = keys.create_key(
            session,
            name=args.name,
            prefix=args.prefix,
            role=args.role,
            note=args.note,
        )
        session.commit()
    except keys.KeyManagementError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    print("=" * 64)
    print(f"key created: {row.name} (role={row.role}, source={row.source})")
    print()
    print(f"    {full_key}")
    print()
    print("This is the ONLY time the full key is shown — store it now.")
    print("Logs will carry the prefix '"
          + row.prefix + "' for attribution, never the key itself.")
    print("=" * 64)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    session = SessionLocal()
    try:
        rows = keys.list_keys(session)
    finally:
        session.close()
    if not rows:
        print("no keys (the environment bootstrap may add some at startup)")
        return 0
    print(f"{'prefix':<18} {'role':<8} {'source':<6} {'status':<9} name / note")
    for row in rows:
        status = "revoked" if row.revoked_at is not None else "active"
        note = f" — {row.note}" if row.note else ""
        print(
            f"{row.prefix:<18} {row.role:<8} {row.source:<6} {status:<9} "
            f"{row.name}{note}"
        )
    print("\n(hash-only storage: the full keys are not recoverable, by design)")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            f"refused: revoking {args.prefix!r} immediately locks out whoever "
            "holds it — pass --yes to confirm",
            file=sys.stderr,
        )
        return 1
    session = SessionLocal()
    try:
        row = keys.revoke_key(session, args.prefix)
        session.commit()
    except keys.KeyManagementError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    print(f"revoked: {row.prefix} ({row.name}) — it stops authenticating immediately")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a key; the full key is printed once")
    create.add_argument("--name", required=True, help="agent/tool name (unique, audit identity)")
    create.add_argument(
        "--prefix",
        required=True,
        help="readable prefix for log attribution, [a-z0-9_-]{2,16} (unique)",
    )
    create.add_argument(
        "--role",
        required=True,
        choices=sorted(keys.ROLES),
        help="permission tier: read < write < admin",
    )
    create.add_argument("--note", help="what this key is for")

    sub.add_parser("list", help="list keys (no key material)")

    revoke = sub.add_parser("revoke", help="revoke a key by prefix")
    revoke.add_argument("prefix", help="prefix of the key to revoke")
    revoke.add_argument("--yes", action="store_true", help="confirm the revocation")

    args = parser.parse_args()
    try:
        if args.command == "create":
            return cmd_create(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "revoke":
            return cmd_revoke(args)
    except SQLAlchemyError as exc:
        print(f"database unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
