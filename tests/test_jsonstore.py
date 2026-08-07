import json

import pytest

from app.jsonstore import (
    JsonCollection,
    JsonStore,
    commits_by_author,
    finite,
    safe_key,
)

COLLECTIONS = {"tech_net": "project_id", "commit_links": "project_id", "react_parent": None}


@pytest.fixture
def store(tmp_path):
    s = JsonStore(root=tmp_path, collections=COLLECTIONS)
    s.tech_net.insert_many([
        {"project_id": "abdera", "project_name": "Abdera", "months": {"1": [["a", "rb", 3]]}},
        {"project_id": "emfdiff/merge", "project_name": "EMF Diff/Merge", "months": {}},
    ])
    s.react_parent.insert_many([{"ReACT-ID": "R1"}, {"ReACT-ID": "R2"}])
    return s


def test_find_one_by_key(store):
    doc = store.tech_net.find_one({"project_id": "abdera"})
    assert doc["months"]["1"] == [["a", "rb", 3]]
    assert store.tech_net.find_one({"project_id": "nope"}) is None


def test_key_with_a_slash_round_trips(store):
    # eclipse_project_info really contains ids like this
    assert store.tech_net.find_one({"project_id": "emfdiff/merge"})["project_name"] == "EMF Diff/Merge"
    assert (store.root / "tech_net" / "emfdiff%2Fmerge.json").exists()


def test_projection(store):
    assert store.tech_net.find_one({"project_id": "abdera"}, {"months": 1, "_id": 0}) == {
        "months": {"1": [["a", "rb", 3]]}
    }
    assert "months" not in store.tech_net.find_one({"project_id": "abdera"}, {"_id": 0, "months": 0})
    with pytest.raises(ValueError):
        store.tech_net.find_one({"project_id": "abdera"}, {"months": 1, "project_id": 0})


def test_find_scan_and_count(store):
    assert len(store.tech_net.find()) == 2
    assert store.tech_net.count_documents({}) == 2
    assert len(store.tech_net.find({"project_name": "Abdera"})) == 1
    assert len(store.react_parent.find()) == 2


def test_secondary_field_filter_still_applies_on_key_lookup(store):
    assert store.tech_net.find_one({"project_id": "abdera", "project_name": "Wrong"}) is None


def test_query_operators_are_rejected_not_ignored(store):
    # returning everything for an unsupported query would be silent data corruption
    with pytest.raises(ValueError):
        store.tech_net.find_one({"project_id": {"$ne": "abdera"}})


def test_replace_one_and_drop(store):
    store.tech_net.replace_one({"project_id": "abdera"}, {"project_id": "abdera", "months": {}})
    assert store.tech_net.find_one({"project_id": "abdera"})["months"] == {}
    store.tech_net.drop()
    assert store.tech_net.count_documents({}) == 0


def test_writes_are_valid_json_and_nan_is_rejected(store):
    with pytest.raises(ValueError):
        store.tech_net.insert_one({"project_id": "bad", "score": float("nan")})
    assert finite({"a": float("nan"), "b": [float("inf"), 1.5]}) == {"a": None, "b": [None, 1.5]}


def test_safe_key_rejects_empty_and_encodes_separators():
    assert safe_key("a/b") == "a%2Fb"
    assert safe_key("..") == ".."          # harmless: the caller appends ".json"
    with pytest.raises(ValueError):
        safe_key("")


def test_commits_by_author_finds_a_persons_work_across_projects(tmp_path):
    coll = JsonCollection(tmp_path, "commit_links", "project_id")
    coll.insert_many([
        {"project_id": "p1", "project_name": "One", "months": {
            "3": [
                {"human_date_time": "Mon Jan 01 2024", "link": "u1",
                 "dealised_author_full_name": "Tom Preston-Werner"},
                {"human_date_time": "Tue Jan 02 2024", "link": "u2",
                 "dealised_author_full_name": "Someone Else"},
            ]}},
        {"project_id": "p2", "project_name": "Two", "months": {
            "7": [{"human_date_time": "Wed Feb 01 2024", "link": "u3",
                   "dealised_author_full_name": "  tom   preston-werner "}]}},
    ])
    hits = commits_by_author(coll, "Tom Preston-Werner")
    assert [h["link"] for h in hits] == ["u1", "u3"]
    assert hits[0]["month"] == "3" and hits[0]["project_name"] == "One"
    assert [h["link"] for h in commits_by_author(coll, "Tom Preston-Werner", project_id="p2")] == ["u3"]
    assert commits_by_author(coll, "Nobody At All") == []


def test_documents_land_on_disk_as_plain_readable_json(store):
    raw = json.loads((store.root / "tech_net" / "abdera.json").read_text())
    assert raw["project_id"] == "abdera"
    assert "_id" not in raw
