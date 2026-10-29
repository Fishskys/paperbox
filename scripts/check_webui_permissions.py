"""Verify real WebUI role visibility with temporary keys, then remove those keys."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services import api_key_service as keys  # noqa: E402


def main() -> None:
    issued: dict[str, str] = {}
    prefixes: list[str] = []
    try:
        with SessionLocal() as session:
            for role in ("read", "write", "admin"):
                prefix = f"webui-test-{role}-{uuid.uuid4().hex[:8]}"
                _, full_key = keys.create_key(session, prefix=prefix, role=role)
                prefixes.append(prefix)
                issued[role] = full_key
            session.commit()
        env = {**os.environ, "PAPERBOX_UI_TEST_KEYS": json.dumps(issued)}
        subprocess.run(
            ["node", str(ROOT / "scripts/check_webui_permissions.mjs")],
            cwd=ROOT, env=env, check=True, timeout=180,
        )
    finally:
        with SessionLocal() as session:
            for prefix in prefixes:
                if keys.get_by_prefix(session, prefix) is not None:
                    keys.revoke_key(session, prefix)
            session.commit()
        print("Temporary WebUI test keys removed.")


if __name__ == "__main__":
    main()
