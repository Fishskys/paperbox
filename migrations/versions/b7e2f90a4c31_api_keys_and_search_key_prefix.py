"""api_keys table + search_queries.key_prefix

Distributable, role-tiered API keys (plan: 2026-10-05_145619-api-auth-keys-roles):

* ``api_keys`` holds one row per credential. Only ``sha256(full_key)`` is stored;
  the full key is printed once at creation. ``name`` / ``prefix`` / ``key_hash``
  are each unique — the prefix is the attribution key that every log line
  carries, so two keys must never share one. ``role`` tiers are
  ``read < write < admin``; ``source`` distinguishes the bootstrap rows derived
  from the environment (re-synced on every startup, not revocable while the env
  references them) from keys created via ``scripts/manage_keys.py``.
* ``search_queries.key_prefix`` records which credential asked for a search,
  so Bad Cases can be attributed (request logs already carry it).

All uniqueness is plain unique constraints (no partial indexes) so the SQLite
unit suite enforces them too.

Revision ID: b7e2f90a4c31
Revises: 8d3f5c1b7a20
Create Date: 2026-10-05 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = 'b7e2f90a4c31'
down_revision: str | None = '8d3f5c1b7a20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_api_keys_name"),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        sa.CheckConstraint(
            "role IN ('read', 'write', 'admin')", name="ck_api_keys_role"
        ),
        sa.CheckConstraint("source IN ('env', 'db')", name="ck_api_keys_source"),
    )
    op.add_column(
        "search_queries",
        sa.Column("key_prefix", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("search_queries", "key_prefix")
    op.drop_table("api_keys")
