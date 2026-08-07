"""Single entry point for data access.

The read-mostly reference collections (per-project forecasts, networks, links,
measures) are served from JSON files; everything still stateful -- users,
sessions, the job queue, tracking -- stays on Mongo for now. Modules should
import ``db`` from here rather than building their own MongoClient, so the
split lives in exactly one place.

Set OSSPREY_JSON_STORE=0 to route everything back to Mongo.
"""

import os

from pymongo import MongoClient

from .config import Config
from .jsonstore import COLLECTIONS, JsonStore

USE_JSON = os.environ.get("OSSPREY_JSON_STORE", "1").lower() not in ("0", "false", "no")

mongo_client = MongoClient(Config.MONGODB_URI)
mongo_db = mongo_client[Config.MONGODB_DB_NAME]
json_store = JsonStore()


class HybridDB:
    """Presents one ``db`` object; picks the backend per collection name."""

    def __init__(self, mongo, store, json_names):
        self._mongo = mongo
        self._store = store
        self._json_names = set(json_names)

    def __getitem__(self, name):
        if USE_JSON and name in self._json_names:
            return self._store[name]
        return self._mongo[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


db = HybridDB(mongo_db, json_store, COLLECTIONS)
