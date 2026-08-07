#!/usr/bin/env python3
"""Paths the endpoint parity run does not reach: the local-mode pipeline reads,
the react_parent consumer, and a full sweep of every project in the two biggest
collections (not just the sample parity.py takes).
"""
import json
import os
import sys
from pathlib import Path

BASE = Path("/mnt/data1/OSSPREY/OSSPREY-BackEnd-Server")
sys.path.insert(0, str(BASE))
os.environ["OSSPREY_JSON_STORE"] = "1"

from dotenv import load_dotenv
load_dotenv(str(BASE / ".env"))
from pymongo import MongoClient

from app.db import db, json_store
from app.jsonstore import commits_by_author

mongo = MongoClient(
    os.environ.get("MONGODB_URI") or "mongodb://localhost:27017",
    serverSelectionTimeoutMS=10000,
)["decal-db"]

fails = []


def check(label, ok, detail=""):
    print("%-58s %s%s" % (label, "OK" if ok else "FAIL", "" if ok else "  " + detail))
    if not ok:
        fails.append(label)


# 1. Local-mode pipeline read path (orchestrator.fetch_project_data_from_db)
from app.pipeline.orchestrator import fetch_project_data_from_db

for pid in ["evidencebot", "reactive", "gitsizer", "does-not-exist"]:
    got = fetch_project_data_from_db(pid)
    want = {}
    for key, coll in (("commit_data", "local_commit_links"), ("issue_data", "local_issue_links")):
        doc = mongo[coll].find_one({"project_id": pid}, {"_id": 0})
        if doc:
            want[key] = doc
    check("orchestrator.fetch_project_data_from_db(%r)" % pid, got == want,
          "keys json=%s mongo=%s" % (sorted(got), sorted(want)))

# 2. Every document in the two biggest collections, not a sample
for name in ("commit_links", "email_links", "eclipse_tech_net", "eclipse_commit_measure"):
    ids = [d["project_id"] for d in mongo[name].find({}, {"project_id": 1, "_id": 0})]
    bad = []
    for pid in ids:
        m = mongo[name].find_one({"project_id": pid}, {"_id": 0})
        j = json_store[name].find_one({"project_id": pid})
        if json.dumps(m, sort_keys=True, default=str) != json.dumps(j, sort_keys=True, default=str):
            bad.append(pid)
    check("%s: all %d documents match mongo" % (name, len(ids)), not bad, str(bad[:3]))

# 3. Commit totals preserved across the whole collection
tot_j = tot_m = 0
for pid in [d["project_id"] for d in mongo.commit_links.find({}, {"project_id": 1, "_id": 0})]:
    for doc, acc in ((json_store.commit_links.find_one({"project_id": pid}), "j"),
                     (mongo.commit_links.find_one({"project_id": pid}, {"_id": 0}), "m")):
        n = sum(len(v or []) for v in (doc.get("months") or {}).values())
        if acc == "j":
            tot_j += n
        else:
            tot_m += n
check("every commit entry preserved (%d)" % tot_j, tot_j == tot_m, "json=%d mongo=%d" % (tot_j, tot_m))

# 4. commits_by_author against a real author from real data
doc = json_store.commit_links.find_one({"project_id": "abdera"})
author = next(
    e["dealised_author_full_name"]
    for entries in doc["months"].values()
    for e in entries
    if e.get("dealised_author_full_name")
)
hits = commits_by_author(json_store.commit_links, author, project_id="abdera")
expected = sum(
    1 for entries in doc["months"].values()
    for e in entries
    if (e.get("dealised_author_full_name") or "").strip().lower() == author.strip().lower()
)
check("commits_by_author(%r) -> %d commits" % (author, len(hits)),
      len(hits) == expected and expected > 0, "expected %d" % expected)

# 5. Endpoints parity.py does not build URLs for
from app import create_app
client = create_app().test_client()
# react_parent has no HTTP route of its own -- it is exported for the ReACT
# tooling, not the dashboard -- so it is checked at the store level below.
for url, key in [("/api/project_info", "projects"), ("/eclipse/project_info", "projects"),
                 ("/api/project_description", None), ("/api/github_stars", None)]:
    r = client.get(url)
    ok = r.status_code == 200 and len(r.get_data()) > 100
    check("GET %-34s %d %dB" % (url, r.status_code, len(r.get_data())), ok)

check("react_parent: %d docs, no HTTP route" % len(json_store.react_parent.find()),
      len(json_store.react_parent.find()) == mongo.react_parent.count_documents({}))

# 6. Non-ASCII / percent-encoded ids survive a real lookup
for pid in ["emfdiff/merge", "cdt(c/c++developmenttooling)"]:
    j = json_store.eclipse_project_info.find_one({"project_id": pid})
    m = mongo.eclipse_project_info.find_one({"project_id": pid}, {"_id": 0})
    check("eclipse_project_info[%r]" % pid, j == m and j is not None)

print()
if fails:
    raise SystemExit("%d checks failed: %s" % (len(fails), fails))
print("all checks passed")
