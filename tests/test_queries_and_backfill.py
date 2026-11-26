"""Query/backfill regressions.

Covers the reported failures:

* ``GET /holds`` racing an ingredient registration / packing submission must
  judge only the shift committed at its first read, and must never write a
  verdict back (which used to poison a freshly submitted packing).
* ``GET /operations`` must not show a stale "held" verdict either.
* Backfilling a previously missing ingredient must correct the affected
  products in ``/operations`` and ``/holds`` immediately, honouring residue and
  cross-line rework, with trace paths reaching the ingredient.
* A failed backfill keeps the previous verdicts.
* An existing (old) SQLite volume is read correctly without migration.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient


def _purge_app_modules() -> None:
    import sys
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    _purge_app_modules()

    from app import database, main
    database.init_db()
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


@pytest.fixture()
def live():
    """Real uvicorn server so requests can genuinely overlap, plus a base URL."""
    import httpx
    import uvicorn

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["DB_PATH"] = tmp.name
    _purge_app_modules()

    from app import database, main
    database.init_db()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(
        main.app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            httpx.get(base + "/status", timeout=0.5)
            break
        except Exception:
            time.sleep(0.02)
    else:  # pragma: no cover - startup failure
        raise RuntimeError("uvicorn did not start")

    class Live:
        url = base

        def request(self, method, path, **kw):
            with httpx.Client(base_url=base, timeout=30) as c:
                return c.request(method, path, **kw)

        def get(self, path, **kw):
            return self.request("GET", path, **kw)

        def post(self, path, **kw):
            return self.request("POST", path, **kw)

    try:
        yield Live()
    finally:
        server.should_exit = True
        thread.join(10)
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def _ingredient(c, batch_no, allergens, name=""):
    r = c.post("/ingredients", json={
        "batch_no": batch_no, "name": name, "allergens": allergens})
    assert r.status_code == 201, r.text
    return r.json()


def _pack(c, seq, line, product, refs, labels):
    r = c.post("/operations/pack", json={
        "seq": seq, "line": line, "product_batch_no": product,
        "refs": refs, "label_allergens": labels})
    assert r.status_code == 201, r.text
    return r.json()


def _clean(c, seq, line, passed):
    r = c.post("/operations/clean", json={"seq": seq, "line": line, "passed": passed})
    assert r.status_code == 201, r.text
    return r.json()


def _hold_items(response):
    return {i["product_batch_no"]: i for i in response.json()["items"]}


def _ops_by_product(c):
    return {o["product_batch_no"]: o for o in c.get("/operations").json()
            if o["op_type"] == "pack"}


def _stored_verdict(product):
    """Read the persisted columns directly, bypassing the API."""
    from app.database import SessionLocal
    from app.models import Operation
    from sqlalchemy import select

    with SessionLocal() as db:
        op = db.scalar(select(Operation).where(Operation.product_batch_no == product))
        return {
            "actual_allergens": op.actual_allergens,
            "on_hold": op.on_hold,
        }


# --------------------------------------------------------------------------
# read-only reads / snapshot semantics
# --------------------------------------------------------------------------
def test_holds_never_writes_verdicts_back(client):
    """A hold-list query is a pure read: stored verdicts stay untouched."""
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "GHOST"}], [])
    before = _stored_verdict("P1")
    assert before["on_hold"] is True  # persisted by the packing submission

    holds = client.get("/holds")
    assert holds.status_code == 200
    assert _hold_items(holds)["P1"]["unknown_refs"] == ["ingredient:GHOST"]
    assert _stored_verdict("P1") == before

    # Same for /operations.
    client.get("/operations")
    assert _stored_verdict("P1") == before


def test_holds_ignores_packing_committed_after_first_read(live, monkeypatch):
    """The reported race: /holds starts, then a clean packing is submitted.

    The in-flight hold list must not contain the new batch at all - neither as
    a real hold nor as a false "unknown ingredient" - and it must not persist a
    bad verdict over the submission's own recompute.  The submission must go
    through while the query is still open.
    """
    from app import crud

    _ingredient(live, "I1", ["egg"])
    _pack(live, 1, "A", "P-A1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])

    computed = threading.Event()
    resume = threading.Event()
    original = crud.build_hold_list
    guard = threading.Lock()

    def paused(packs):
        # Everything for this answer is already read; freeze here while the
        # operator registers I-MILK and submits a packing.  Only the first
        # caller (the /holds request) pauses; the writer keeps running.
        if guard.acquire(blocking=False):
            try:
                computed.set()
                assert resume.wait(20), "writer did not finish in time"
            finally:
                guard.release()
        return original(packs)

    monkeypatch.setattr(crud, "build_hold_list", paused)

    result: dict = {}

    def read_holds():
        try:
            result["holds"] = live.get("/holds")
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            result["error"] = exc

    reader = threading.Thread(target=read_holds)
    reader.start()
    assert computed.wait(20)

    _ingredient(live, "I-MILK", ["milk"])
    submitted = _pack(live, 2, "B", "P-B1",
                      [{"kind": "ingredient", "ref_no": "I-MILK"}], ["milk"])
    assert submitted["actual_allergens"] == ["milk"]
    assert submitted["on_hold"] is False
    resume.set()
    reader.join(20)

    assert "error" not in result, result.get("error")
    holds = result["holds"]
    assert holds.status_code == 200
    # Snapshot taken before the submission: P-B1 is simply not part of it.
    assert "P-B1" not in _hold_items(holds)
    # And no stale verdict was written over the submission's computed one.
    assert _stored_verdict("P-B1") == {"actual_allergens": ["milk"], "on_hold": False}

    # A later query sees the committed shift and still holds nothing.
    assert _hold_items(live.get("/holds")) == {}
    assert _ops_by_product(live)["P-B1"]["actual_allergens"] == ["milk"]


def test_holds_uses_snapshot_of_ingredients_not_partially_loaded(live, monkeypatch):
    """A registration committed between the ingredient and operation reads is
    either fully in or fully out - never a half-visible shift."""
    from app import crud

    _ingredient(live, "I1", ["egg"])
    _pack(live, 1, "A", "P-A1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])

    snapshot_read = threading.Event()
    resume = threading.Event()
    original = crud._load_ingredients
    guard = threading.Lock()

    def paused(db):
        rows = original(db)
        if guard.acquire(blocking=False):
            try:
                snapshot_read.set()
                assert resume.wait(20)
            finally:
                guard.release()
        return rows

    monkeypatch.setattr(crud, "_load_ingredients", paused)

    result: dict = {}

    def read_holds():
        try:
            result["holds"] = live.get("/holds")
        except Exception as exc:  # pragma: no cover
            result["error"] = exc

    reader = threading.Thread(target=read_holds)
    reader.start()
    assert snapshot_read.wait(20)

    # Commit an ingredient and a packing that uses it, both after the snapshot.
    _ingredient(live, "I2", ["milk"])
    _pack(live, 2, "B", "P-B2",
          [{"kind": "ingredient", "ref_no": "I2"}], [])
    resume.set()
    reader.join(20)

    assert "error" not in result, result.get("error")
    items = _hold_items(result["holds"])
    assert "P-B2" not in items, "packing not committed at snapshot time leaked in"

    # The packing's own submission-time verdict is intact (milk unlabelled).
    stored = _stored_verdict("P-B2")
    assert stored["on_hold"] is True
    fresh = _hold_items(live.get("/holds"))["P-B2"]
    assert fresh["missing_on_label"] == ["milk"]
    assert fresh["unknown_refs"] == []


def test_reads_correct_previously_wrong_persisted_verdicts(client):
    """Rows written by an older/racy version must not be trusted verbatim."""
    from app.database import SessionLocal
    from app.models import Operation, PackRef
    from sqlalchemy import select

    _ingredient(client, "I1", ["egg"])
    with SessionLocal() as db:
        # Simulate an old volume: a pack whose stored verdict says "clean",
        # even though its ref points at GHOST and the label omits egg.
        op = Operation(seq=1, line="A", op_type="pack", product_batch_no="P-OLD",
                       label_allergens=[], actual_allergens=[], actual_paths=[],
                       on_hold=False)
        op.refs = [PackRef(kind="ingredient", ref_no="I1"),
                   PackRef(kind="ingredient", ref_no="GHOST")]
        db.add(op)
        db.commit()

    item = _hold_items(client.get("/holds"))["P-OLD"]
    assert item["unknown_refs"] == ["ingredient:GHOST"]
    assert item["missing_on_label"] == ["egg"]
    assert item["missing_fields"] == ["label_allergens"]

    op_out = _ops_by_product(client)["P-OLD"]
    assert op_out["on_hold"] is True
    assert op_out["actual_allergens"] == ["egg"]
    assert ["P-OLD", "ingredient:I1", "egg"] in op_out["actual_paths"]


def test_old_volume_in_rollback_journal_mode_is_readable():
    """An existing DB created before WAL was enabled keeps working."""
    from sqlalchemy import create_engine, text

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_engine = create_engine(f"sqlite:///{tmp.name}", future=True)
    with old_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=DELETE")
        conn.exec_driver_sql(
            "CREATE TABLE ingredient_batches (id INTEGER PRIMARY KEY, batch_no VARCHAR(64) "
            "UNIQUE, name VARCHAR(128), allergens JSON)")
        conn.exec_driver_sql(
            "CREATE TABLE operations (id INTEGER PRIMARY KEY, seq INTEGER, line VARCHAR(16), "
            "op_type VARCHAR(16), product_batch_no VARCHAR(64), label_allergens JSON, "
            "actual_allergens JSON, actual_paths JSON, on_hold BOOLEAN, passed BOOLEAN)")
        conn.exec_driver_sql(
            "CREATE TABLE pack_refs (id INTEGER PRIMARY KEY, operation_id INTEGER, "
            "kind VARCHAR(16), ref_no VARCHAR(64))")
        conn.exec_driver_sql(
            "INSERT INTO ingredient_batches (batch_no, name, allergens) "
            "VALUES ('I1', '', '[\"egg\"]')")
        conn.exec_driver_sql(
            "INSERT INTO operations (seq, line, op_type, product_batch_no, label_allergens, "
            "actual_allergens, actual_paths, on_hold) VALUES "
            "(1, 'A', 'pack', 'P-OLD', '[]', '[]', '[]', 0)")
        conn.exec_driver_sql(
            "INSERT INTO pack_refs (operation_id, kind, ref_no) VALUES (1, 'ingredient', 'I1')")
        conn.commit()
    old_engine.dispose()

    os.environ["DB_PATH"] = tmp.name
    _purge_app_modules()
    from app import database, main
    database.init_db()
    with TestClient(main.app) as c:
        s = database.SessionLocal()
        assert s.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        s.close()
        item = _hold_items(c.get("/holds"))["P-OLD"]
        assert item["missing_on_label"] == ["egg"]
        assert item["unknown_refs"] == []
        ops = _ops_by_product(c)["P-OLD"]
        assert ops["actual_allergens"] == ["egg"]
        assert ops["on_hold"] is True
    os.unlink(tmp.name)


# --------------------------------------------------------------------------
# ingredient backfill
# --------------------------------------------------------------------------
def test_backfill_updates_operations_and_holds_without_hold_query(client):
    """Filling a previously missing ingredient corrects the verdicts at once."""
    _ingredient(client, "I1", ["egg"])
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    _pack(client, 2, "B", "P2",
          [{"kind": "ingredient", "ref_no": "I-MISSING"}], [])

    before = _ops_by_product(client)["P2"]
    assert before["on_hold"] is True
    assert before["actual_allergens"] == []

    _ingredient(client, "I-MISSING", ["milk", "soy"])

    # No /holds call in between: /operations alone must already be corrected.
    after = _ops_by_product(client)["P2"]
    assert after["actual_allergens"] == ["milk", "soy"]
    assert after["on_hold"] is True  # still unlabelled
    assert ["P2", "ingredient:I-MISSING", "milk"] in after["actual_paths"]
    assert _stored_verdict("P2")["on_hold"] is True

    item = _hold_items(client.get("/holds"))["P2"]
    assert item["unknown_refs"] == []
    assert item["missing_on_label"] == ["milk", "soy"]
    assert item["missing_fields"] == ["label_allergens"]
    assert ["P2", "ingredient:I-MISSING", "milk"] in item["trace_paths"]
    assert ["P2", "ingredient:I-MISSING", "soy"] in item["trace_paths"]


def test_backfill_propagates_through_residue_and_cross_line_rework(client):
    """Affected products are re-judged with residue and cross-line rework.

    Line A: P-A1(egg, labelled) -> P-A2(backfilled sesame) -> P-A3(residue).
    Line B: P-B1 reworks P-A2 across lines.  After the backfill all three
    affected products are held and every trace reaches the ingredient.
    """
    _ingredient(client, "I1", ["egg"])
    _pack(client, 1, "A", "P-A1",
          [{"kind": "ingredient", "ref_no": "I1"}], ["egg"])
    _clean(client, 2, "A", True)  # clear line-A residue before the backfilled pack
    _pack(client, 3, "A", "P-A2",
          [{"kind": "ingredient", "ref_no": "I-X"}], [])
    _pack(client, 4, "B", "P-B1",
          [{"kind": "rework", "ref_no": "P-A2"}], [])
    _pack(client, 5, "A", "P-A3", [], [])  # inherits line-A residue from P-A2

    pre = _ops_by_product(client)
    assert pre["P-A2"]["actual_allergens"] == []
    assert pre["P-B1"]["actual_allergens"] == []
    assert pre["P-A3"]["actual_allergens"] == []

    _ingredient(client, "I-X", ["sesame"])

    ops = _ops_by_product(client)
    assert ops["P-A2"]["actual_allergens"] == ["sesame"]
    assert ops["P-B1"]["actual_allergens"] == ["sesame"]
    assert ops["P-A3"]["actual_allergens"] == ["sesame"]
    assert ["P-B1", "rework:P-A2", "ingredient:I-X", "sesame"] in ops["P-B1"]["actual_paths"]
    assert ["P-A3", "residue:P-A2", "ingredient:I-X", "sesame"] in ops["P-A3"]["actual_paths"]

    holds = _hold_items(client.get("/holds"))
    assert holds["P-A2"]["unknown_refs"] == []
    assert holds["P-B1"]["unknown_refs"] == []
    assert ["P-B1", "rework:P-A2", "ingredient:I-X", "sesame"] in holds["P-B1"]["trace_paths"]
    assert ["P-A3", "residue:P-A2", "ingredient:I-X", "sesame"] in holds["P-A3"]["trace_paths"]
    # The already-correct P-A1 is unaffected.
    assert "P-A1" not in holds


def test_backfill_failure_keeps_previous_verdicts(client):
    _pack(client, 1, "A", "P1",
          [{"kind": "ingredient", "ref_no": "I-X"}], [])
    before_hold = _hold_items(client.get("/holds"))["P1"]
    assert before_hold["unknown_refs"] == ["ingredient:I-X"]
    before_ops = _ops_by_product(client)["P1"]

    _ingredient(client, "I-X", ["milk"])  # succeeds
    dup = client.post("/ingredients",
                      json={"batch_no": "I-X", "name": "", "allergens": ["soy"]})
    assert dup.status_code == 409  # duplicate backfill attempt

    # A rejected duplicate changes nothing (the original allergens stay).
    assert _hold_items(client.get("/holds"))["P1"]["unknown_refs"] == []
    assert _ops_by_product(client)["P1"]["actual_allergens"] == ["milk"]
    assert dup.json()["detail"].startswith("ingredient batch I-X already exists")

    # Now simulate an internal failure while backfilling another ingredient.
    from app import crud

    _pack(client, 2, "B", "P2",
          [{"kind": "ingredient", "ref_no": "I-Y"}], [])
    ops_before = _ops_by_product(client)["P2"]

    def boom(*_a, **_kw):
        raise RuntimeError("simulated recompute failure")

    original = crud.recompute_and_persist
    crud.recompute_and_persist = boom
    try:
        r = client.post("/ingredients", json={"batch_no": "I-Y", "allergens": ["soy"]})
        assert r.status_code == 500
    finally:
        crud.recompute_and_persist = original

    # Ingredient was not created and P2 keeps its previous unknown-ref verdict.
    assert not any(i["batch_no"] == "I-Y" for i in client.get("/ingredients").json())
    assert _ops_by_product(client)["P2"]["on_hold"] is True
    assert _ops_by_product(client)["P2"]["actual_allergens"] == ops_before["actual_allergens"]
    assert _hold_items(client.get("/holds"))["P2"]["unknown_refs"] == ["ingredient:I-Y"]
