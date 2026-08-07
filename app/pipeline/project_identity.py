"""How a git URL becomes a project, and what stops two repos becoming one.

The pipeline identifies a project by the repository name alone, so
gin-gonic/gin and codegangsta/gin both resolve to "gin" -- one project_id, one
pair of scraper output filenames, one stored record. Whichever is processed
second silently replaces the first. These helpers are what let the API refuse
that instead.
"""


def extract_owner_repo(git_link):
    """Extract the (owner, repo) pair from a git URL.

    e.g. "https://github.com/RepoWise/frontend.git" -> ("RepoWise", "frontend")
    """
    link = git_link[:-4] if git_link.endswith(".git") else git_link
    parts = link.rstrip("/").split("/")
    repo = parts[-1] if parts else ""
    owner = parts[-2] if len(parts) >= 2 else ""
    return owner, repo


def extract_project_name(git_link):
    """Bare repository name.

    This matches the file names the scraper writes (e.g.
    "frontend-commit-file-dev.csv"), so it is used for locating those CSVs.
    """
    return extract_owner_repo(git_link)[1]


def generate_project_id(project_name):
    """Generate a project_id by removing non-alphanumeric characters and lowercasing."""
    return "".join(c for c in project_name if c.isalnum()).lower()


def repo_key(git_link):
    """Canonical "owner/repo" for a git URL, case-insensitive."""
    owner, repo = extract_owner_repo(git_link)
    return f"{owner}/{repo}".lower()


def project_source(db, project_id):
    """Which repository the stored project came from, or None if unknown.

    Prefers the recorded ``git_link``. Projects processed before that field
    existed have none, so fall back to the owner/repo embedded in any stored
    commit URL -- that makes the check work on the existing projects without a
    migration.
    """
    doc = db.local_commit_links.find_one({"project_id": project_id})
    if not doc:
        return None
    if doc.get("git_link"):
        return repo_key(doc["git_link"])
    for entries in (doc.get("months") or {}).values():
        for entry in entries or []:
            link = (entry or {}).get("link") or ""
            parts = link.split("/")
            if "github.com" in link and len(parts) > 4:
                return f"{parts[3]}/{parts[4]}".lower()
    return None


def collision_with(db, git_link):
    """The repo already occupying this project_id, when it is a different one.

    Returns None when the id is free or already belongs to this same repository.
    """
    incoming = repo_key(git_link)
    existing = project_source(db, generate_project_id(extract_project_name(git_link)))
    if existing and existing != incoming:
        return existing
    return None
