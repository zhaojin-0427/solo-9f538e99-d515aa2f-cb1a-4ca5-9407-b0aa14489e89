"""End-to-end API tests, including the cross-line rework cleaning correction."""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    # Drop cached app modules so the package re-imports against the temp DB.
    import sys
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]

    from app import database, main
    database.init_db()
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def _ingredient(client, batch_no, allergens, name=""):
    r = client.post("/ingredients", json={
        "batch_no": batch_no, "name": name, "allergens": allergens})
    assert r.status_code == 201, r.text
    return r.json()


def _pack(client, seq, line, product, refs, labels):
    r = client.post("/operations/pack", json={
        "seq": seq, "line": line, "product_batch_no": product,
        "refs": refs, "label_allergens": labels})
    assert r.status_code == 201, r.text
    return r.json()


def _clean(client, seq, line, passed):
    r = client.post("/operations/clean", json={
        "seq": seq, "line": line, "passed": passed})
    assert r.status_code == 201, r.text
    return r.json()


def test_ingredient_and_empty_status(client):
    _ingredient(client, "I1", ["egg"])
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["ingredient_count"] == 1


def test_direct_ingredient_allergen_and_label_gap(client):
    _ingredient(client, "I1", ["egg", "milk"])
    pack = _pack(client, 1, "A", "P1",
                 [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    assert pack["actual_allergens"] == ["egg", "milk"]
    holds = client.get("/holds").json()
    item = holds["items"][0]
    assert item["product_batch_no"] == "P1"
    assert item["missing_on_label"] == ["milk"]
    # path reaches the root ingredient
    path = next(p for p in item["trace_paths"] if p[-1] == "milk")
    assert path == ["P1", "ingredient:I1", "milk"]


def test_missing_fields_hold(client):
    _ingredient(client, "I1", [])
    _pack(client, 1, "A", "P1", [], [])
    item = client.get("/holds").json()["items"][0]
    assert item["missing_fields"] == ["materials", "label_allergens"]


def test_unknown_refs_reported(client):
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "GHOST"}], [])
    item = client.get("/holds").json()["items"][0]
    assert item["unknown_refs"] == ["ingredient:GHOST"]


def test_seq_conflict_and_duplicate_product(client):
    _ingredient(client, "I1", [])
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], [])
    r = client.post("/operations/pack", json={
        "seq": 1, "line": "B", "product_batch_no": "P2",
        "refs": [{"kind": "ingredient", "ref_no": "I1"}],
        "label_allergens": []})
    assert r.status_code == 409
    r = client.post("/operations/pack", json={
        "seq": 2, "line": "B", "product_batch_no": "P1",
        "refs": [{"kind": "ingredient", "ref_no": "I1"}],
        "label_allergens": []})
    assert r.status_code == 409


def test_residue_cleaning_and_rework_survival(client):
    _ingredient(client, "I1", ["egg"])
    # P1 on line A carries egg and leaves egg residue on A
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    # failed cleaning: residue stays
    _clean(client, 2, "A", False)
    # P2 has no allergen input but inherits line-A residue
    p2 = _pack(client, 3, "A", "P2", [], [])
    assert p2["actual_allergens"] == ["egg"]
    # passed cleaning clears residue; P3 is clean
    _clean(client, 4, "A", True)
    p3 = _pack(client, 5, "A", "P3", [], [])
    assert p3["actual_allergens"] == []
    # but reworking pre-cleaning P1 still brings egg back
    p4 = _pack(client, 6, "B", "P4",
               [{"kind": "rework", "ref_no": "P1"}], [])
    assert p4["actual_allergens"] == ["egg"]
    paths = p4["actual_paths"]
    assert ["P4", "rework:P1", "ingredient:I1", "egg"] in paths


def test_cross_line_rework_cleaning_correction(client):
    """Full required scenario.

    Line A: P1(egg) -> clean(seq2) -> P2(no input) ; Line B later reworks P2.
    First the cleaning passes: no residue, P2 clean, cross-line rework clean.
    Correct it to failed: residue re-appears, P2 and every transitive rework
    descendant (on B) must show up as examined batches and as hold items.
    """
    _ingredient(client, "I1", ["egg"])
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    _clean(client, 2, "A", True)                       # initially passes
    p2 = _pack(client, 3, "A", "P2", [], [])
    assert p2["actual_allergens"] == []
    # line B reworks P2 across lines, then reworks its own output again
    p3 = _pack(client, 4, "B", "P3",
               [{"kind": "rework", "ref_no": "P2"}], [])
    p4 = _pack(client, 5, "B", "P4",
               [{"kind": "rework", "ref_no": "P3"}], [])
    assert p3["actual_allergens"] == []
    assert p4["actual_allergens"] == []
    assert client.get("/holds").json()["held_batch_count"] >= 1  # missing fields

    # correct cleaning seq 2: pass -> fail
    r = client.post("/operations/clean/2/correct", json={"passed": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["examined_batch_nos"] == ["P2", "P3", "P4"]
    assert body["examined_count"] == 3

    # hold list regenerated together with the correction
    held = {i["product_batch_no"]: i for i in body["hold_list"]["items"]}
    assert set(held) >= {"P2", "P3", "P4"}
    assert held["P2"]["missing_on_label"] == ["egg"]
    # full trace from P4 back across rework + residue to the ingredient
    rework_path = ["P4", "rework:P3", "rework:P2", "residue:P1",
                   "ingredient:I1", "egg"]
    assert rework_path in held["P4"]["trace_paths"]
    # line-B residue left by P3 is an equally valid source
    residue_path = ["P4", "residue:P3", "rework:P2", "residue:P1",
                    "ingredient:I1", "egg"]
    assert residue_path in held["P4"]["trace_paths"]

    # correction is durable
    ops = {o["seq"]: o for o in client.get("/operations").json()}
    assert ops[2]["passed"] is False
    assert ops[5]["actual_allergens"] == ["egg"]

    # correcting back to pass must clear everything again
    r2 = client.post("/operations/clean/2/correct", json={"passed": True})
    assert r2.json()["examined_batch_nos"] == ["P2", "P3", "P4"]
    ops = {o["seq"]: o for o in client.get("/operations").json()}
    assert ops[3]["actual_allergens"] == []
    assert ops[5]["actual_allergens"] == []


def test_correction_unchanged_keeps_result(client):
    _ingredient(client, "I1", ["egg"])
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    _clean(client, 2, "A", True)
    _pack(client, 3, "A", "P2", [], [])
    r = client.post("/operations/clean/2/correct", json={"passed": True})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["examined_batch_nos"] == ["P2"]


def test_correction_404(client):
    r = client.post("/operations/clean/99/correct", json={"passed": True})
    assert r.status_code == 404
    r = client.post("/operations/clean/1/correct", json={"passed": True})
    assert r.status_code == 404


def test_failed_correction_keeps_previous_result(client, monkeypatch):
    """If recomputation blows up mid-correction, the old result is retained."""
    from app import crud

    _ingredient(client, "I1", ["egg"])
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    _clean(client, 2, "A", True)
    _pack(client, 3, "A", "P2", [], [])

    ops = {o["seq"]: o for o in client.get("/operations").json()}
    assert ops[2]["passed"] is True
    assert ops[3]["actual_allergens"] == []

    def boom(*_a, **_kw):
        raise RuntimeError("simulated recompute failure")

    monkeypatch.setattr(crud, "recompute_and_persist", boom)
    r = client.post("/operations/clean/2/correct", json={"passed": False})
    assert r.status_code == 500

    monkeypatch.undo()
    ops = {o["seq"]: o for o in client.get("/operations").json()}
    # previous conclusion and verdicts are still in force
    assert ops[2]["passed"] is True
    assert ops[3]["actual_allergens"] == []
    assert ops[3]["on_hold"] is True  # missing-fields hold from before


def test_start_of_shift_no_residue(client):
    # both lines start clean: a pack with no inputs carries nothing
    p = _pack(client, 1, "B", "P1", [], ["egg"])
    assert p["actual_allergens"] == []
