"""Build the per-developer commit and issue link tables for a processed repo.

Clicking a node in the Social or Technical Network opens "the commits/issues for
this person, this month". That lookup is by (project, month, author name), so
the three have to agree with the network exactly:

* **Month** is calendar months since the project's start date, the same rule the
  forecaster uses (``months_fill`` with ``strat="month"`` in
  ``decalfc/abstractions/rawdata.py``), reading the same cached start date from
  ``ospex-ref/start_dates.json``. Deriving it any other way -- from the CSV's
  ``incubation_month`` column, or from the earliest row -- silently produces a
  different numbering and the drilldown comes back empty.
* **Author name** is the CSV's ``name`` verbatim. The network node labels are
  drawn from the same column, so they match without normalisation; measured at
  100% of nodes across gem5, jekyll, hugo, celery, ReACTive and EvidenceBot.

Written from the same scraper CSVs, in the same pipeline pass, that produced the
networks -- so the two cannot drift apart.
"""

import csv
import json
import logging
import os
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

START_DATES = os.environ.get(
    "OSSPREY_START_DATES",
    "/mnt/data1/OSSPREY/OSSPREY-Pex-Forecaster/ospex-ref/start_dates.json",
)

# Commit messages and issue bodies blow past the default field limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %Z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
)


def parse_date(value):
    value = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def project_start(project_name, rows, date_field):
    """The month-0 anchor: the forecaster's cached start date if it has one."""
    try:
        with open(START_DATES, "r", encoding="utf-8") as fh:
            cached = json.load(fh).get(project_name)
        if cached:
            return int(cached[:4]), int(cached[5:7])
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("start-date cache unavailable (%s); falling back to earliest row", exc)
    dates = [d for d in (parse_date(r.get(date_field)) for r in rows) if d]
    if not dates:
        return None
    first = min(dates)
    return first.year, first.month


def month_index(dt, anchor):
    year0, month0 = anchor
    return (dt.year - year0) * 12 + (dt.month - month0)


def _read(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        # Some scraped issue bodies carry embedded NULs, which the csv module
        # refuses outright ("line contains NUL"). Drop them; they are never
        # part of a date, a URL or a name.
        return list(csv.DictReader(line.replace("\0", "") for line in fh))


def build_links(csv_path, project_id, project_name, kind, git_link=None):
    """{project_id, project_name, git_link, last_fetched, months: {"<m>": [...]}}.

    ``kind`` is "commit" or "issue"; returns None when there is nothing to store.
    ``git_link`` records which repository the rows came from, so a later job for
    a same-named repository under a different owner can be told apart.
    """
    date_field = "date" if kind == "commit" else "created_at"
    author_field = "name" if kind == "commit" else "user_name"
    link_field = "commit_url" if kind == "commit" else "issue_url"

    rows = _read(csv_path)
    if not rows:
        return None
    anchor = project_start(project_name, rows, date_field)
    if anchor is None:
        logger.warning("%s: no parseable %s dates in %s", project_name, kind, csv_path)
        return None

    months = {}
    seen = set()
    skipped = 0
    for row in rows:
        dt = parse_date(row.get(date_field))
        if not dt:
            skipped += 1
            continue
        link = row.get(link_field) or ""
        author = row.get(author_field) or ""
        month = str(month_index(dt, anchor))
        # The commit CSV has one row per file changed, and the issue CSV one per
        # comment, so a single commit touching 200 files would otherwise appear
        # 200 times in the dialog (and made gem5's table 25 MB).
        key = (month, author, link)
        if link and key in seen:
            continue
        seen.add(key)
        months.setdefault(month, []).append({
            "human_date_time": dt.strftime("%a %b %d %H:%M:%S %Y"),
            "link": link,
            "dealised_author_full_name": author,
        })

    if skipped:
        logger.warning("%s %s: %d rows had an unparseable date", project_name, kind, skipped)
    logger.info("%s %s links: %d entries across %d months",
                project_name, kind, sum(len(v) for v in months.values()), len(months))
    return {
        "project_id": project_id,
        "project_name": project_name,
        "git_link": git_link,
        "last_fetched": datetime.utcnow().strftime("%a %b %d %H:%M:%S %Y"),
        "months": months,
    }


def store_links(db, tech_csv, social_csv, project_id, project_name, git_link=None):
    """Build and persist both tables. Never raises: a failure here must not take
    the repository-processing pipeline down with it."""
    written = {}
    for csv_path, kind, collection in (
        (tech_csv, "commit", "local_commit_links"),
        (social_csv, "issue", "local_issue_links"),
    ):
        try:
            doc = build_links(csv_path, project_id, project_name, kind, git_link)
            if doc is None:
                continue
            db[collection].replace_one({"project_id": project_id}, doc, upsert=True)
            written[collection] = sum(len(v) for v in doc["months"].values())
        except Exception as exc:  # noqa: BLE001 - best effort by design
            logger.error("failed to store %s for %s: %s", collection, project_name, exc)
    return written
