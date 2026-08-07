import csv
import json

from app.pipeline.commit_issue_links import build_links, month_index, project_start


def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


COMMIT_COLS = ["name", "date", "commit_url", "filename"]


def test_month_is_calendar_months_from_the_cached_start(tmp_path, monkeypatch):
    starts = tmp_path / "start_dates.json"
    starts.write_text(json.dumps({"proj": "2003-10-07 14:41:54+00:00"}))
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(starts))
    anchor = project_start("proj", [], "date")
    assert anchor == (2003, 10)
    import datetime
    assert month_index(datetime.datetime(2003, 10, 31), anchor) == 0
    assert month_index(datetime.datetime(2004, 1, 1), anchor) == 3
    assert month_index(datetime.datetime(2026, 7, 28), anchor) == 273


def test_one_entry_per_commit_not_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(tmp_path / "none.json"))
    csv_path = tmp_path / "c.csv"
    # the scraper emits one row per file touched by a commit
    write_csv(csv_path, [
        {"name": "Gabe Black", "date": "2003-10-07 14:41:54 UTC", "commit_url": "u1", "filename": "a.cc"},
        {"name": "Gabe Black", "date": "2003-10-07 14:41:54 UTC", "commit_url": "u1", "filename": "b.hh"},
        {"name": "Gabe Black", "date": "2003-11-02 10:00:00 UTC", "commit_url": "u2", "filename": "c.py"},
    ], COMMIT_COLS)
    doc = build_links(str(csv_path), "proj", "proj", "commit")
    assert [e["link"] for e in doc["months"]["0"]] == ["u1"]
    assert [e["link"] for e in doc["months"]["1"]] == ["u2"]
    assert doc["months"]["0"][0]["dealised_author_full_name"] == "Gabe Black"


def test_falls_back_to_earliest_row_without_a_cached_start(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(tmp_path / "missing.json"))
    csv_path = tmp_path / "c.csv"
    write_csv(csv_path, [
        {"name": "A", "date": "2020-05-01 00:00:00 UTC", "commit_url": "u1", "filename": "x"},
        {"name": "B", "date": "2020-07-01 00:00:00 UTC", "commit_url": "u2", "filename": "y"},
    ], COMMIT_COLS)
    doc = build_links(str(csv_path), "proj", "proj", "commit")
    assert sorted(doc["months"]) == ["0", "2"]


def test_embedded_nuls_and_unparseable_dates_do_not_abort(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(tmp_path / "missing.json"))
    csv_path = tmp_path / "c.csv"
    csv_path.write_bytes(
        b"name,date,commit_url,filename\n"
        b"A,2020-05-01 00:00:00 UTC,u1,x\n"
        b"B,not-a-date,u2,y\n"
        b"C\x00,2020-05-02 00:00:00 UTC,u3,z\n"
    )
    doc = build_links(str(csv_path), "proj", "proj", "commit")
    assert [e["link"] for e in doc["months"]["0"]] == ["u1", "u3"]
    assert doc["months"]["0"][1]["dealised_author_full_name"] == "C"


def test_issue_rows_use_their_own_columns(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(tmp_path / "missing.json"))
    csv_path = tmp_path / "i.csv"
    write_csv(csv_path, [
        {"user_name": "Jason Lowe-Power", "created_at": "2020-05-01T00:00:00Z", "issue_url": "i1"},
        {"user_name": "Jason Lowe-Power", "created_at": "2020-05-09T00:00:00Z", "issue_url": "i1"},
    ], ["user_name", "created_at", "issue_url"])
    doc = build_links(str(csv_path), "proj", "proj", "issue")
    assert [e["link"] for e in doc["months"]["0"]] == ["i1"]


def test_empty_or_missing_csv_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pipeline.commit_issue_links.START_DATES", str(tmp_path / "missing.json"))
    assert build_links(str(tmp_path / "nope.csv"), "p", "p", "commit") is None
    empty = tmp_path / "e.csv"
    write_csv(empty, [], COMMIT_COLS)
    assert build_links(str(empty), "p", "p", "commit") is None
