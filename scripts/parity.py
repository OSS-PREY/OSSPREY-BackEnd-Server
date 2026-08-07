#!/usr/bin/env python3
"""Every JSON-backed endpoint, answered from JSON vs from Mongo, compared.

Runs the Flask app twice in-process -- once with OSSPREY_JSON_STORE=1 and once
with 0 -- and diffs the responses. A pass means the migration is invisible to
the frontend.
"""
import json
import os
import random
import sys
from pathlib import Path

BASE = Path("/mnt/data1/OSSPREY/OSSPREY-BackEnd-Server")
sys.path.insert(0, str(BASE))


def sample_ids(collection, n=6):
    d = BASE / "data/json" / collection
    files = sorted(f.stem for f in d.glob("*.json") if f.name != "_meta.json")
    if not files:
        return []
    random.seed(collection)
    picks = files[:2] + random.sample(files, min(n - 2, len(files)))
    from urllib.parse import unquote
    return [unquote(p) for p in dict.fromkeys(picks)]


def months_for(collection, pid, n=3):
    """A few real month keys plus one that does not exist (404 path)."""
    from urllib.parse import quote
    f = BASE / "data/json" / collection / (quote(pid, safe="") + ".json")
    try:
        doc = json.loads(f.read_text())
    except Exception:
        return [1, 999999]
    keys = sorted((doc.get("months") or {}), key=lambda k: int(k))[:n]
    return [int(k) for k in keys] + [999999]


def build_urls():
    urls = [
        "/api/projects", "/api/eclipse_projects", "/api/monthly_ranges",
        "/api/github_repositories", "/api/apache_project_description",
    ]
    per_month = [
        ("tech_net", "/api/tech_net/%s/%d"),
        ("social_net", "/api/social_net/%s/%d"),
        ("commit_links", "/api/commit_links/%s/%d"),
        ("email_links", "/api/email_links/%s/%d"),
        ("commit_measure", "/api/commit_measure/%s/%d"),
        ("email_measure", "/api/email_measure/%s/%d"),
        ("eclipse_tech_net", "/eclipse/tech_net/%s/%d"),
        ("eclipse_social_net", "/eclipse/social_net/%s/%d"),
        ("eclipse_email_links", "/eclipse/email_links/%s/%d"),
        ("eclipse_commit_measure", "/eclipse/commit_measure/%s/%d"),
        ("eclipse_email_measure", "/eclipse/email_measure/%s/%d"),
        ("eclipse_issue_measure", "/eclipse/issue_measure/%s/%d"),
    ]
    for coll, tmpl in per_month:
        for pid in sample_ids(coll, 4):
            for m in months_for(coll, pid, 2):
                urls.append(tmpl % (pid, m))
    for coll, tmpl in [("project_info", "/api/project_info/%s"),
                       ("grad_forecast", "/api/grad_forecast/%s"),
                       ("eclipse_grad_forecast", "/eclipse/grad_forecast/%s")]:
        for pid in sample_ids(coll, 5):
            urls.append(tmpl % pid)
    urls.append("/api/project_info/definitely-not-a-project")
    return urls


def responses(use_json, urls):
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    os.environ["OSSPREY_JSON_STORE"] = "1" if use_json else "0"
    from app import create_app
    client = create_app().test_client()
    out = {}
    for u in urls:
        r = client.get(u)
        out[u] = (r.status_code, r.get_data(as_text=True))
    return out


# Differences that are known, deliberate and checked to be unobservable to the
# frontend. Anything not listed here is a failure.
def normalise(url, status, body):
    """Fold in the two accepted differences, loudly and in one place.

    1. Order. Keyed collections are read back in filename order, Mongo returns
       insertion order. Every consumer of a list endpoint looks its entry up by
       project_id (projectStore.js:761 and :567), so order is not observable.
    2. monthly_ranges.last_updated. A BSON datetime rendered as an HTTP-date by
       Mongo, ISO-8601 from JSON. The field is never read by the frontend.
    """
    try:
        doc = json.loads(body)
    except ValueError:
        return status, body
    for key in ("project_ranges", "projects", "repositories", "descriptions"):
        items = doc.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            for item in items:
                item.pop("last_updated", None)
            ident = next((k for k in ("project_id", "name") if k in items[0]), None)
            if ident:
                doc[key] = sorted(items, key=lambda d: str(d.get(ident)))
    return status, json.dumps(doc, sort_keys=True)


def main():
    urls = build_urls()
    print("comparing %d endpoint responses\n" % len(urls))
    a = responses(True, urls)
    b = responses(False, urls)
    bad = []
    for u in urls:
        a[u] = normalise(u, *a[u])
        b[u] = normalise(u, *b[u])
        if a[u] != b[u]:
            bad.append(u)
            print("DIFF %s\n  json : %s %s\n  mongo: %s %s"
                  % (u, a[u][0], a[u][1][:160], b[u][0], b[u][1][:160]))
    print()
    if bad:
        raise SystemExit("%d of %d endpoints differ" % (len(bad), len(urls)))
    codes = {}
    for u in urls:
        codes[a[u][0]] = codes.get(a[u][0], 0) + 1
    print("all %d identical  (status codes: %s)" % (len(urls), codes))


if __name__ == "__main__":
    main()
