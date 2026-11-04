#!/usr/bin/env python3
"""Exercise all migrations in a disposable PostgreSQL schema, always cleaned up."""

import argparse
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    from app.db.models import Base

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="PAPERBOX_MIGRATION_TEST_DSN", help="explicit test DSN environment variable; never reads .env")
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"set {args.dsn_env} to an explicit test PostgreSQL DSN")
    schema = f"verify_migrations_{uuid4().hex}"
    engine = create_engine(dsn)
    with engine.connect() as connection:
        if set(inspect(connection).get_table_names(schema="public")) & set(Base.metadata.tables):
            engine.dispose()
            raise RuntimeError("refusing an application database; supply a separate empty test database")
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = make_url(dsn).update_query_dict({"options": f"-csearch_path={schema}"})
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.set_main_option("version_table_schema", schema)
        config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
        script = ScriptDirectory.from_config(config)
        assert len(script.get_heads()) == 1, "migration history must have a single head"
        for cycle in range(2):
            command.upgrade(config, "head")
            with engine.connect() as connection:
                actual = set(inspect(connection).get_table_names(schema=schema))
                assert set(Base.metadata.tables) <= actual, "migrations do not create all model tables"
                for name, table in Base.metadata.tables.items():
                    columns = {column["name"] for column in inspect(connection).get_columns(name, schema=schema)}
                    assert set(table.columns.keys()) <= columns, f"missing columns in {name}"
                head = connection.execute(text(f'SELECT version_num FROM "{schema}".alembic_version')).scalar_one()
                assert head == script.get_current_head()
            if cycle == 0:
                command.downgrade(config, "base")
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()
    print("Migration upgrade, downgrade and second upgrade passed; temporary schema cleaned up.")


if __name__ == "__main__":
    main()