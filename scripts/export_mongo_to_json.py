#!/usr/bin/env python3
"""Export the read-mostly Mongo collections to the JSON store.

Non-destructive: Mongo is only read. Every document is re-read from disk and
compared against what came out of Mongo before the collection is reported as
exported, so a partial or lossy write is a failure, not a surprise later.

    python scripts/export_mongo_to_json.py            # export everything
    python scripts/export_mongo_to_json.py tech_net   # just these collections
    python scripts/export_mongo_to_json.py --verify   # re-check, write nothing
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from pymongo import MongoClient

from app.jsonstore import COLLECTIONS, JsonCollection, default_root, finite, safe_key


def normalise(value):
    """What a document looks like after a JSON round trip."""
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, (list, tuple)):
        return [normalise(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return finite(value)


def export_collection(mongo_db, root, name, key, verify_only=False):
    coll = JsonCollection(root, name, key)
    source = list(mongo_db[name].find({}, {"_id": 0}))
    expected = [normalise(d) for d in source]

    if not verify_only:
        if key:
            coll.path.mkdir(parents=True, exist_ok=True)
            for doc in expected:
                if key not in doc:
                    raise SystemExit(f"{name}: document without '{key}': {list(doc)[:5]}")
                coll._write_atomic(coll.path / f"{safe_key(doc[key])}.json", doc)
            coll._write_atomic(
                coll.path / "_meta.json",
                {
                    "key": key,
                    "count": len(expected),
                    "exported_at": datetime.utcnow().isoformat(),
                    "source": f"mongo:{mongo_db.name}.{name}",
                },
            )
        else:
            coll._write_atomic(coll.path, expected)

    # --- verify: read back through the store and compare, document by document
    if key:
        got = {d[key]: d for d in coll.find()}
        want = {d[key]: d for d in expected}
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        differing = sorted(k for k in want if k in got and got[k] != want[k])
        ok = not (missing or extra or differing)
        detail = ""
        if not ok:
            detail = " missing=%s extra=%s differing=%s" % (
                missing[:3], extra[:3], differing[:3],
            )
    else:
        got = coll.find()
        ok = got == expected
        detail = "" if ok else " (%d on disk vs %d in mongo)" % (len(got), len(expected))

    size = 0
    if key and coll.path.is_dir():
        size = sum(f.stat().st_size for f in coll.path.glob("*.json"))
    elif coll.path.exists():
        size = coll.path.stat().st_size

    print(
        "%-24s %-5d docs  %7.1f MB  %s%s"
        % (name, len(expected), size / 1e6, "OK" if ok else "MISMATCH", detail)
    )
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("collections", nargs="*", help="default: all")
    ap.add_argument("--verify", action="store_true", help="compare only, write nothing")
    ap.add_argument("--root", default=None, help="target directory")
    args = ap.parse_args()

    load_dotenv(str(Path(__file__).resolve().parent.parent / ".env"))
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI") or "mongodb://localhost:27017"
    mongo_db = MongoClient(uri, serverSelectionTimeoutMS=10000)["decal-db"]

    root = Path(args.root) if args.root else default_root()
    names = args.collections or list(COLLECTIONS)
    unknown = [n for n in names if n not in COLLECTIONS]
    if unknown:
        raise SystemExit(f"not JSON-backed collections: {unknown}")

    print(f"{'verifying' if args.verify else 'exporting'} -> {root}\n")
    failures = [
        n for n in names
        if not export_collection(mongo_db, root, n, COLLECTIONS[n], args.verify)
    ]
    print()
    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print(f"all {len(names)} collections verified")


if __name__ == "__main__":
    main()
