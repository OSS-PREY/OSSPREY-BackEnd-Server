"""JSON-file backing store for the read-mostly reference collections.

These collections are only ever read by project_id or scanned whole -- no
aggregations, no indexes, no joins -- so a file per project is a faithful
replacement for a Mongo collection and removes the server from the deployment.

Layout, rooted at OSSPREY_DATA_DIR (default <backend>/data/json):

    <collection>/_meta.json         {"key": "project_id", "count": N, ...}
    <collection>/<key>.json         one document, verbatim minus _id
    <collection>.json               keyless collections: a single JSON array

Only the pymongo surface the app actually uses is implemented. Anything else
raises, so an unsupported query fails loudly instead of quietly returning the
wrong rows.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# collection -> primary key field. None means "keyless": the whole collection
# lives in one array file and is only ever scanned.
COLLECTIONS = {
    "commit_links": "project_id",
    "email_links": "project_id",
    "eclipse_commit_links": "project_id",
    "eclipse_email_links": "project_id",
    "tech_net": "project_id",
    "social_net": "project_id",
    "eclipse_tech_net": "project_id",
    "eclipse_social_net": "project_id",
    "project_info": "project_id",
    "eclipse_project_info": "project_id",
    "monthly_ranges": "project_id",
    "grad_forecast": "project_id",
    "eclipse_grad_forecast": "project_id",
    "commit_measure": "project_id",
    "email_measure": "project_id",
    "eclipse_commit_measure": "project_id",
    "eclipse_email_measure": "project_id",
    "eclipse_issue_measure": "project_id",
    "local_commit_links": "project_id",
    "local_issue_links": "project_id",
    "apache_projects": None,
    "github_repositories": None,
    "react_parent": None,
}


def default_root():
    env = os.environ.get("OSSPREY_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "json"


def json_default(value):
    """Serialise the few non-JSON types these documents carry."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def finite(value):
    """Replace NaN/Infinity with None, recursively.

    Mongo happily stores float("nan") -- project_info/samoa has one in
    "mentor" -- but NaN is not valid JSON, and writing it produces a file every
    strict parser rejects, the browser included. A missing value is what it
    means.
    """
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def safe_key(value):
    """Filename stem for a document key.

    This is a path built from data, so it is percent-encoded rather than used
    raw: eclipse_project_info really does contain ids like "emfdiff/merge".
    Encoding is reversible and leaves separators and NULs no way through, and
    the ".json" suffix the caller appends makes "." and ".." ordinary names.
    """
    key = str(value)
    if not key:
        raise ValueError("empty document key")
    return quote(key, safe="")


def _project(doc, projection):
    """Apply a pymongo-style projection. Only include/exclude maps are used."""
    if not projection or doc is None:
        return doc
    # '_id' is never stored, so drop it from the spec before deciding the mode.
    spec = {k: v for k, v in projection.items() if k != "_id"}
    if not spec:
        return dict(doc)
    includes = [k for k, v in spec.items() if v]
    excludes = [k for k, v in spec.items() if not v]
    if includes and excludes:
        raise ValueError(f"mixed include/exclude projection: {projection!r}")
    if includes:
        return {k: doc[k] for k in includes if k in doc}
    return {k: v for k, v in doc.items() if k not in excludes}


def _matches(doc, filt):
    if not filt:
        return True
    for field, want in filt.items():
        if isinstance(want, dict):
            raise ValueError(f"query operators are not supported: {field}={want!r}")
        if doc.get(field) != want:
            return False
    return True


class JsonCollection:
    """A Mongo-collection-shaped view over a directory of JSON documents."""

    def __init__(self, root, name, key):
        self.name = name
        self.key = key
        self.root = Path(root)
        # keyed collections get a directory; keyless ones a single array file
        self.path = self.root / name if key else self.root / f"{name}.json"
        self._lock = threading.Lock()

    # ---------------- reading ----------------
    def _read(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.error("jsonstore: cannot read %s: %s", path, exc)
            return None

    def _all(self):
        """Every document, in a stable order."""
        if not self.key:
            return list(self._read(self.path) or [])
        if not self.path.is_dir():
            return []
        docs = []
        for f in sorted(self.path.glob("*.json")):
            if f.name == "_meta.json":
                continue
            doc = self._read(f)
            if isinstance(doc, dict):
                docs.append(doc)
        return docs

    def find_one(self, filter=None, projection=None):
        filt = dict(filter or {})
        # The fast path: a lookup by primary key is a single file read.
        if self.key and self.key in filt and not isinstance(filt[self.key], dict):
            doc = self._read(self.path / f"{safe_key(filt.pop(self.key))}.json")
            if doc is None or not _matches(doc, filt):
                return None
            return _project(doc, projection)
        for doc in self._all():
            if _matches(doc, filt):
                return _project(doc, projection)
        return None

    def find(self, filter=None, projection=None):
        return [
            _project(doc, projection) for doc in self._all() if _matches(doc, filter)
        ]

    def count_documents(self, filter=None):
        if not filter and self.key and self.path.is_dir():
            return sum(1 for f in self.path.glob("*.json") if f.name != "_meta.json")
        return len(self.find(filter))

    # ---------------- writing ----------------
    def _write_atomic(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, default=json_default, allow_nan=False)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _doc_path(self, doc):
        if self.key not in doc:
            raise ValueError(f"{self.name}: document is missing '{self.key}'")
        return self.path / f"{safe_key(doc[self.key])}.json"

    def insert_one(self, document):
        doc = {k: v for k, v in document.items() if k != "_id"}
        with self._lock:
            if self.key:
                self._write_atomic(self._doc_path(doc), doc)
            else:
                self._write_atomic(self.path, (self._read(self.path) or []) + [doc])

    def insert_many(self, documents):
        docs = [{k: v for k, v in d.items() if k != "_id"} for d in documents]
        with self._lock:
            if self.key:
                for doc in docs:
                    self._write_atomic(self._doc_path(doc), doc)
            else:
                self._write_atomic(self.path, (self._read(self.path) or []) + docs)

    def replace_one(self, filter, replacement, upsert=False):
        doc = {k: v for k, v in replacement.items() if k != "_id"}
        if self.key and self.key in (filter or {}):
            # keyed replace is just a write to that document's path
            doc.setdefault(self.key, filter[self.key])
            with self._lock:
                self._write_atomic(self._doc_path(doc), doc)
            return
        with self._lock:
            docs = self._read(self.path) or []
            for i, existing in enumerate(docs):
                if _matches(existing, filter):
                    docs[i] = doc
                    break
            else:
                if not upsert:
                    return
                docs.append(doc)
            self._write_atomic(self.path, docs)

    def drop(self):
        with self._lock:
            if self.key:
                if self.path.is_dir():
                    for f in self.path.glob("*.json"):
                        if f.name != "_meta.json":
                            f.unlink()
            elif self.path.exists():
                self.path.unlink()


class JsonStore:
    """Hands out JsonCollections; ``store.tech_net`` / ``store['tech_net']``."""

    def __init__(self, root=None, collections=None):
        self.root = Path(root) if root else default_root()
        self._keys = dict(collections or COLLECTIONS)
        self._cache = {}

    def __getitem__(self, name):
        if name not in self._keys:
            raise KeyError(f"{name} is not a JSON-backed collection")
        if name not in self._cache:
            self._cache[name] = JsonCollection(self.root, name, self._keys[name])
        return self._cache[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __contains__(self, name):
        return name in self._keys


def commits_by_author(collection, author, project_id=None):
    """Every commit (or issue) a person is credited with, newest file order.

    ``collection`` is any *_links collection -- commit_links, email_links, or
    their eclipse/local variants. Matching is case- and whitespace-insensitive
    on ``dealised_author_full_name``, the same name the dashboard shows.

    Returns [{project_id, project_name, month, link, date, author}].
    """
    wanted = " ".join(str(author).split()).lower()
    docs = (
        [collection.find_one({"project_id": project_id})]
        if project_id
        else collection.find()
    )
    out = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for month, entries in (doc.get("months") or {}).items():
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("dealised_author_full_name") or ""
                if " ".join(str(name).split()).lower() != wanted:
                    continue
                out.append(
                    {
                        "project_id": doc.get("project_id"),
                        "project_name": doc.get("project_name"),
                        "month": month,
                        "link": entry.get("link"),
                        "date": entry.get("human_date_time"),
                        "author": name,
                    }
                )
    return out
