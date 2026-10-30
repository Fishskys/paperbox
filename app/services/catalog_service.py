"""Model-free metadata predicates and query-scoped, paper-counting facets."""

from sqlalchemy import String, cast, func, literal, or_, select, union_all

from app.db.models import Author, Paper, PaperAuthor, Venue

SCOPES = ("all", "title", "authors", "venue", "doi", "arxiv_id", "abstract")
SORTS = ("relevance", "newest", "year_desc", "year_asc", "title")


def metadata_predicate(scope: str, word: str):
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    fields = {"title": Paper.title, "abstract": Paper.abstract, "doi": Paper.doi, "arxiv_id": Paper.arxiv_id}
    predicates = {name: column.ilike(pattern, escape="\\") for name, column in fields.items()}
    predicates["authors"] = Paper.id.in_(select(PaperAuthor.paper_id).join(Author).where(Author.name.ilike(pattern, escape="\\")))
    predicates["venue"] = Paper.venue_id.in_(select(Venue.id).where(Venue.name.ilike(pattern, escape="\\")))
    if scope == "all":
        # Separate indexed legs let PostgreSQL use each field's trigram index.
        legs = [select(Paper.id).where(Paper.deleted_at.is_(None), predicate)
                for predicate in predicates.values()]
        return Paper.id.in_(union_all(*legs))
    return predicates[scope]


def metadata_facets(session, statement) -> dict:
    filtered = statement.with_only_columns(Paper.id, Paper.year, Paper.paper_type, Paper.venue_id).order_by(None).cte("filtered_papers")
    queries = []
    for name, column in (("year", filtered.c.year), ("paper_type", filtered.c.paper_type)):
        queries.append(select(literal(name).label("facet"), cast(column, String).label("key"), func.count().label("count"))
            .where(column.is_not(None)).group_by(column))
    queries.append(select(literal("venue").label("facet"), Venue.name.label("key"), func.count().label("count"))
        .select_from(filtered.join(Venue, Venue.id == filtered.c.venue_id)).group_by(Venue.name))
    queries.append(select(literal("authors").label("facet"), Author.name.label("key"), func.count(func.distinct(filtered.c.id)).label("count"))
        .select_from(filtered.join(PaperAuthor, PaperAuthor.paper_id == filtered.c.id).join(Author, Author.id == PaperAuthor.author_id)).group_by(Author.name))
    buckets = union_all(*queries).subquery()
    ranked = select(buckets, func.row_number().over(partition_by=buckets.c.facet,
        order_by=(buckets.c.count.desc(), buckets.c.key)).label("position")).subquery()
    result = {name: [] for name in ("year", "paper_type", "venue", "authors")}
    for row in session.execute(select(ranked).where(ranked.c.position <= 50).order_by(ranked.c.facet, ranked.c.position)).mappings():
        result[row["facet"]].append({"key": row["key"], "count": row["count"]})
    return result