from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.cleanup import CleanupIn, IndexClaimIn


def test_new_cleanup_is_opt_in_and_history_is_not_a_category():
    assert Settings(_env_file=None).cleanup_auto_enabled is False
    with pytest.raises(ValidationError):
        CleanupIn(request_id=uuid4(), categories=["metadata_history"])


@pytest.mark.parametrize("extra", [
    {"dry_run": False}, {"categories": ["deleted_papers"]},
    {"categories": ["indices"]}, {"categories": ["jobs", "jobs"]},
    {"categories": ["deleted_papers", "jobs"], "paper_ids": [uuid4()]},
    {"categories": ["jobs"], "paper_ids": [uuid4()]},
    {"operation": "inventory", "dry_run": False, "preview_job_id": uuid4()},
    {"preview_job_id": uuid4()},
    {"dry_run": False, "preview_job_id": uuid4(), "retry_job_id": uuid4()},
])
def test_cleanup_rejects_implicit_or_conflicting_scope(extra):
    with pytest.raises(ValidationError):
        CleanupIn.model_validate({"request_id": uuid4(), "categories": ["jobs"], **extra})


@pytest.mark.parametrize("name", ["*", "paper_*", "a,b", "_all", "../foo", "A", "x/y", "x?y", "x y"])
def test_index_cleanup_does_not_accept_patterns_or_paths(name):
    with pytest.raises(ValidationError):
        IndexClaimIn(name=name)


def test_custom_index_names_do_not_need_a_version_suffix():
    assert IndexClaimIn(name="research-papers-202611").name == "research-papers-202611"
    request = CleanupIn(request_id=uuid4(), categories=["deleted_papers"], paper_ids=[uuid4()])
    assert request.dry_run