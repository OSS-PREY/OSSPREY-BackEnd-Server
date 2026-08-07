#!/usr/bin/env python3
"""Build commit/issue link tables for every repo that already has networks.

For each project with both a scraper CSV and a net-vis cache, the links are
rebuilt and then *verified against the network itself*: for every month, every
node label in the technical/social network must resolve to at least one link.
A project below the threshold is reported and not written -- a drilldown that
silently returns someone else's commits is worse than an empty one.

    python scripts/backfill_links.py                # verify, write what passes
    python scripts/backfill_links.py --dry-run      # verify only
    python scripts/backfill_links.py gem5 jekyll    # named projects
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.db import db
from app.pipeline.commit_issue_links import build_links

SCRAPER_OUT = Path(os.environ.get(
    "OSSPREY_SCRAPER_OUT", "/mnt/data1/OSSPREY/OSSPREY-OSS-Scraper-Tool/output"))
NET_VIS = Path(os.environ.get(
    "OSSPREY_NET_VIS", "/mnt/data1/OSSPREY/OSSPREY-Pex-Forecaster/net-vis"))
MIN_COVERAGE = 0.95


def generate_project_id(project_name):
    """Same rule as the pipeline (orchestrator.generate_project_id)."""
    return "".join(c for c in project_name if c.isalnum()).lower()


def node_coverage(doc, net, side):
    """Fraction of network node labels that have at least one link that month."""
    if not doc:
        return 0.0, 0, 0
    have = defaultdict(set)
    for month, entries in doc["months"].items():
        for e in entries:
            if e["dealised_author_full_name"]:
                have[month].add(e["dealised_author_full_name"])
    found = total = 0
    for month, rows in (net.get(side) or {}).items():
        names = {r[0] for r in rows if isinstance(r, list) and len(r) >= 3}
        if not names:
            continue
        found += len(names & have.get(str(month), set()))
        total += len(names)
    return (found / total if total else 1.0), found, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = args.projects or sorted(f.stem for f in NET_VIS.glob("*.json"))
    print("%-22s %-26s %-26s %s" % ("project", "commit links", "issue links", "written"))
    written = skipped = nodata = 0
    for name in names:
        net_file = NET_VIS / f"{name}.json"
        tech_csv = SCRAPER_OUT / f"{name}-commit-file-dev.csv"
        social_csv = SCRAPER_OUT / f"{name}_issues.csv"
        if not net_file.exists() or not tech_csv.exists():
            print("%-22s (no net-vis cache or no commit CSV)" % name[:22])
            nodata += 1
            continue
        net = json.loads(net_file.read_text())
        pid = generate_project_id(name)

        cdoc = build_links(str(tech_csv), pid, name, "commit")
        idoc = build_links(str(social_csv), pid, name, "issue") if social_csv.exists() else None

        ccov, cf, ct = node_coverage(cdoc, net, "tech")
        icov, if_, it = node_coverage(idoc, net, "social")

        ok = ccov >= MIN_COVERAGE and (it == 0 or icov >= MIN_COVERAGE)
        if ok and not args.dry_run:
            if cdoc:
                db.local_commit_links.replace_one({"project_id": pid}, cdoc, upsert=True)
            if idoc:
                db.local_issue_links.replace_one({"project_id": pid}, idoc, upsert=True)
            written += 1
        elif not ok:
            skipped += 1

        print("%-22s %5.1f%% of %-5d nodes      %5.1f%% of %-5d nodes      %s" % (
            name[:22], 100 * ccov, ct, 100 * icov, it,
            "yes" if (ok and not args.dry_run) else ("dry-run" if ok else "NO - below threshold")))

    print("\n%d written, %d skipped below %.0f%% coverage, %d without data"
          % (written, skipped, 100 * MIN_COVERAGE, nodata))


if __name__ == "__main__":
    main()
