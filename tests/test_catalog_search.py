"""Metadata scopes, query-scoped facets and stable pagination require no models."""

import pytest

from app.db.models import Paper
from app.services import paper_service
from tests.test_paper_list_filters import add_paper, client, factory  # noqa: F401


def seed(factory):
    first = add_paper(factory, title="Clock design", venue="ISSCC", year=2024, paper_type="conference")
    second = add_paper(factory, title="Other work", venue="JSSC", year=2020, paper_type="journal")
    with factory() as session:
        paper = session.get(Paper, first)
        paper.doi = "10.1234/timing"
        paper.abstract = "A precision timing circuit"
        paper_service.set_paper_authors(session, paper, ["Alice Chen"])
        session.commit()
    return first, second


@pytest.mark.parametrize("scope,query", [("title", "clock"), ("authors", "Alice"), ("venue", "ISSCC"), ("doi", "10.1234/timing"), ("abstract", "precision")])
def test_metadata_scopes(client, factory, scope, query):
    first, _ = seed(factory)
    payload = client.get("/api/papers", params={"scope": scope, "q": query}).json()
    assert [row["paper_id"] for row in payload["papers"]] == [first]


def test_all_scope_can_match_words_across_fields_and_facets_follow_the_query(client, factory, monkeypatch):
    from app.services import search_pipeline, embedding_service, rerank_service
    for module, name in ((search_pipeline, "maybe_rewrite"), (embedding_service, "embed_text"), (rerank_service, "rerank_texts")):
        monkeypatch.setattr(module, name, lambda *a, **kw: pytest.fail("metadata search must not call models"))
    first, _ = seed(factory)
    payload = client.get("/api/papers", params={"scope": "all", "q": "clock Alice", "facets": True}).json()
    assert payload["total"] == 1
    assert payload["papers"][0]["paper_id"] == first
    assert payload["facets"]["venue"] == [{"key": "ISSCC", "count": 1}]
    assert payload["facets"]["authors"] == [{"key": "Alice Chen", "count": 1}]


def test_sort_pages_and_scope_validation(client, factory):
    first, second = seed(factory)
    payload = client.get("/api/papers?sort=year_asc&limit=1&offset=1").json()
    assert payload["total"] == 2
    assert payload["papers"][0]["paper_id"] == first
    assert client.get("/api/papers?scope=unknown").status_code == 422