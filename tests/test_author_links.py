"""Author links survive being written twice (reindex/reprocess regression).

Reindexing a paper re-runs metadata extraction, which calls
``paper_service.set_paper_authors`` again. Before this guard the second call hit
``uq_paper_authors_paper_author`` and the whole reindex job failed.
"""

from __future__ import annotations

import sqlalchemy
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Author, Paper, PaperAuthor, new_uuid
from app.services import paper_service


@pytest.fixture()
def session():
    """In-memory SQLite with the three tables author handling needs."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.types import JSON

    dialect_types = {JSONB: JSON(), UUID: sqlalchemy.String(36)}
    for table in (Paper.__table__, Author.__table__, PaperAuthor.__table__):
        columns = [column._copy() for column in table.columns]
        for column in columns:
            for source, replacement in dialect_types.items():
                if isinstance(column.type, source):
                    column.type = replacement
        meta = sqlalchemy.MetaData()
        sqlalchemy.Table(table.name, meta, *columns)
        meta.create_all(engine)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def make_paper(session) -> Paper:
    paper = Paper(
        id=new_uuid(),
        title="Low Power SRAM",
        fingerprint=f"sha256:{new_uuid()}",
        status="PENDING",
    )
    session.add(paper)
    session.commit()
    return paper


def test_setting_authors_twice_keeps_one_link_per_author(session) -> None:
    paper = make_paper(session)

    paper_service.set_paper_authors(session, paper, ["Alice", "Bob"])
    session.commit()
    paper_service.set_paper_authors(session, paper, ["Alice", "Bob"])
    session.commit()

    links = (
        session.query(PaperAuthor)
        .filter(PaperAuthor.paper_id == paper.id)
        .order_by(PaperAuthor.author_order)
        .all()
    )
    assert len(links) == 2
    assert paper_service.paper_author_names(paper) == ["Alice", "Bob"]


def test_replacing_authors_drops_the_old_links(session) -> None:
    paper = make_paper(session)

    paper_service.set_paper_authors(session, paper, ["Alice", "Bob"])
    session.commit()
    paper_service.set_paper_authors(session, paper, ["Carol"])
    session.commit()

    assert paper_service.paper_author_names(paper) == ["Carol"]
    assert session.query(Author).filter(Author.name == "Carol").count() == 1
