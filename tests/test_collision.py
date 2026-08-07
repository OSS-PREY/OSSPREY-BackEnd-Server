"""The guard that stops two same-named repositories becoming one project."""

from app.pipeline.project_identity import (
    collision_with,
    extract_project_name,
    generate_project_id,
    project_source,
    repo_key,
)

GIN_GONIC = "https://github.com/gin-gonic/gin.git"
CODEGANGSTA = "https://github.com/codegangsta/gin.git"


class FakeDB:
    """Just enough of the store: one local_commit_links document."""

    def __init__(self, doc):
        self.doc = doc
        self.local_commit_links = self

    def find_one(self, filt):
        if self.doc and self.doc.get("project_id") == filt.get("project_id"):
            return self.doc
        return None


RECORDED = {"project_id": "gin", "git_link": GIN_GONIC, "months": {}}
# processed before git_link was stored: the source has to be inferred
LEGACY = {"project_id": "gin", "months": {"3": [
    {"link": "https://github.com/gin-gonic/gin/commit/abc123",
     "dealised_author_full_name": "x"},
]}}


def test_same_name_different_owner_collapses_to_one_id():
    assert generate_project_id(extract_project_name(GIN_GONIC)) == "gin"
    assert generate_project_id(extract_project_name(CODEGANGSTA)) == "gin"
    assert repo_key(GIN_GONIC) == "gin-gonic/gin"
    assert repo_key(CODEGANGSTA) == "codegangsta/gin"


def test_other_owner_is_refused():
    assert collision_with(FakeDB(RECORDED), CODEGANGSTA) == "gin-gonic/gin"


def test_same_repo_is_allowed_to_reprocess():
    assert collision_with(FakeDB(RECORDED), GIN_GONIC) is None


def test_case_and_git_suffix_are_ignored():
    assert collision_with(FakeDB(RECORDED), "https://github.com/Gin-Gonic/Gin") is None


def test_legacy_document_source_is_inferred_from_commit_urls():
    """The 25 existing projects predate git_link, so the guard must work without it."""
    assert project_source(FakeDB(LEGACY), "gin") == "gin-gonic/gin"
    assert collision_with(FakeDB(LEGACY), CODEGANGSTA) == "gin-gonic/gin"
    assert collision_with(FakeDB(LEGACY), GIN_GONIC) is None


def test_unknown_project_has_nothing_to_collide_with():
    assert collision_with(FakeDB(None), CODEGANGSTA) is None


def test_unknowable_source_allows_rather_than_blocks():
    """No git_link and no usable commit URL: don't block a legitimate reprocess."""
    blank = {"project_id": "gin", "months": {"1": [
        {"link": "", "dealised_author_full_name": "x"}]}}
    assert project_source(FakeDB(blank), "gin") is None
    assert collision_with(FakeDB(blank), CODEGANGSTA) is None


def test_different_names_never_collide():
    assert collision_with(FakeDB(RECORDED), "https://github.com/vuejs/core.git") is None
