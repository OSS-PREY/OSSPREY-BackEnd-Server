"""Content-addressed answer cache for the LLM-backed endpoints.

The key is the SHA-256 of whatever fully determines the answer -- for pain
points the fully rendered prompt (which folds in project name, digest,
documents and the missing-docs list), for actionables the retrieval profile
and wanted categories. Any change to the inputs changes the key, so stale
entries are never served; they just sit unread.

SQLite on disk rather than the in-memory dict this replaces: gunicorn runs
with ``reload = True``, so every code edit restarts the worker, and an
in-process cache was wiped on each deploy. The database lives next to the
JSON store under ``data/``. No TTL -- correctness comes from the key, not
from expiry.

Only successful answers are ever written by the callers; failures propagate
and leave no entry.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get(
    'OSSPREY_ANSWER_CACHE',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'data', 'answer_cache.db'))

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS answers ('
        '  key TEXT PRIMARY KEY,'
        '  payload TEXT NOT NULL,'
        '  created REAL NOT NULL,'
        '  hits INTEGER NOT NULL DEFAULT 0'
        ')')
    return conn


def key_for(*parts):
    """SHA-256 over the parts, NUL-separated so boundaries cannot collide."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()


def get(key):
    """The stored payload dict, or None. A read bumps the hit counter."""
    try:
        # ``with conn`` commits but never closes; wrap in closing() so the
        # connection is released rather than left to the garbage collector.
        with _lock, closing(_connect()) as conn, conn:
            row = conn.execute(
                'SELECT payload FROM answers WHERE key = ?', (key,)).fetchone()
            if row is None:
                return None
            conn.execute('UPDATE answers SET hits = hits + 1 WHERE key = ?', (key,))
        return json.loads(row[0])
    except (sqlite3.Error, ValueError) as e:
        # A broken cache must never take the endpoint down with it.
        logger.warning(f'answer cache: read failed: {e}')
        return None


def put(key, payload):
    try:
        with _lock, closing(_connect()) as conn, conn:
            conn.execute(
                'INSERT OR REPLACE INTO answers (key, payload, created, hits)'
                ' VALUES (?, ?, ?, 0)',
                (key, json.dumps(payload), time.time()))
    except (sqlite3.Error, TypeError, ValueError) as e:
        logger.warning(f'answer cache: write failed: {e}')
