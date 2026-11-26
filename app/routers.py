"""FastAPI routes for the central-kitchen allergen hold system.

Read endpoints (``GET /holds``, ``GET /operations``) are snapshot reads: they
compute verdicts from the shift data committed at their first database read and
never write them back.  Write endpoints take SQLite's write lock up front
(``BEGIN IMMEDIATE``) and persist recomputed verdicts in the same transaction,
so a concurrent read can neither block them nor expose a half-written shift.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud
from .database import get_db, get_write_db
from .engine import affected_closure
from .models import IngredientBatch, Operation
from .schemas import (
    CorrectCleanIn,
    CorrectCleanOut,
    HoldListOut,
    IngredientIn,
    IngredientOut,
    OperationOut,
    PackIn,
    CleanIn,
    StatusOut,
)

router = APIRouter()


# ---------------- ingredients ----------------
@router.post("/ingredients", response_model=IngredientOut, status_code=201,
             tags=["ingredients"])
def create_ingredient(data: IngredientIn, db: Session = Depends(get_write_db)):
    """Register an ingredient batch.

    This is also the backfill path for a batch that packing referenced while it
    was still missing: in the same transaction the full shift is re-judged, so
    ``GET /operations`` and ``GET /holds`` both correct the affected products
    without waiting for a hold-list query.  A failure (including a duplicate
    batch number) rolls back and keeps the previous verdicts in force.
    """
    try:
        if db.scalar(select(IngredientBatch).where(IngredientBatch.batch_no == data.batch_no)):
            raise HTTPException(409, f"ingredient batch {data.batch_no} already exists")
        row = IngredientBatch(**data.model_dump())
        db.add(row)
        db.flush()
        crud.recompute_and_persist(db)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"ingredient batch {data.batch_no} already exists")
    except Exception:
        # Backfill failed: undo the registration and every recomputed verdict,
        # so the previous judgement stays in force.
        db.rollback()
        raise HTTPException(500, "ingredient backfill failed; previous verdicts retained")
    db.refresh(row)
    return row


@router.get("/ingredients", response_model=list[IngredientOut], tags=["ingredients"])
def list_ingredients(db: Session = Depends(get_db)):
    return list(db.scalars(select(IngredientBatch).order_by(IngredientBatch.id)).all())


# ---------------- operations ----------------
def _check_seq_free(db: Session, seq: int) -> None:
    if db.scalar(select(Operation.id).where(Operation.seq == seq)):
        raise HTTPException(409, f"seq {seq} already used; operations must stay shift-ordered")


@router.post("/operations/pack", response_model=OperationOut, status_code=201,
             tags=["operations"])
def create_pack(data: PackIn, db: Session = Depends(get_write_db)):
    _check_seq_free(db, data.seq)
    if db.scalar(select(Operation.id).where(
            Operation.product_batch_no == data.product_batch_no)):
        raise HTTPException(409, f"product batch {data.product_batch_no} already exists")
    try:
        op = crud.add_pack(db, data)
        db.flush()
        packs = crud.recompute_and_persist(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "conflicting operation")
    pack = next(p for p in packs if p.product_batch_no == data.product_batch_no)
    return crud.serialize_operation(op, pack)


@router.post("/operations/clean", response_model=OperationOut, status_code=201,
             tags=["operations"])
def create_clean(data: CleanIn, db: Session = Depends(get_write_db)):
    _check_seq_free(db, data.seq)
    try:
        op = crud.add_clean(db, data)
        db.flush()
        crud.recompute_and_persist(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "conflicting operation")
    return crud.serialize_operation(op)


@router.get("/operations", response_model=list[OperationOut], tags=["operations"])
def list_operations(db: Session = Depends(get_db)):
    """All operations with live verdicts from this request's snapshot.

    Verdicts are recomputed from the raw inputs (ingredients, refs, labels,
    cleaning conclusions), so rows stored by an older volume or left stale by a
    not-yet-backfilled reference are still reported correctly.
    """
    packs, _ = crud.compute(db)
    packs_by_product = {p.product_batch_no: p for p in packs}
    ops = db.scalars(select(Operation).order_by(Operation.seq)).all()
    return [
        crud.serialize_operation(
            op,
            packs_by_product.get(op.product_batch_no)
            if op.op_type == "pack" else None,
        )
        for op in ops
    ]


# ---------------- hold list ----------------
@router.get("/holds", response_model=HoldListOut, tags=["holds"])
def get_holds(db: Session = Depends(get_db)):
    """Hold list computed from the snapshot pinned at first read. Read-only."""
    packs, _ = crud.compute(db)
    return crud.build_hold_list(packs)


# ---------------- cleaning correction ----------------
@router.post("/operations/clean/{seq}/correct", response_model=CorrectCleanOut,
             tags=["correction"])
def correct_cleaning(seq: int, data: CorrectCleanIn, db: Session = Depends(get_write_db)):
    """Correct one cleaning conclusion.

    Only batches reachable afterwards (same-line residue chain, then transitive
    rework including across lines) are re-examined and their batch numbers
    returned. The new conclusion and the regenerated hold list take effect in
    one transaction; on failure the previous result stays in force.
    """
    op = db.scalar(select(Operation).where(Operation.seq == seq))
    if op is None or op.op_type != "clean":
        raise HTTPException(404, f"cleaning operation with seq {seq} not found")

    previous_passed = bool(op.passed)
    changed = previous_passed != data.passed

    try:
        if changed:
            op.passed = data.passed
        # Full-shift recompute inside one transaction; rollback restores both
        # the cleaning conclusion and every previously persisted verdict.
        packs = crud.recompute_and_persist(db)

        edges = crud.get_rework_edges(packs)
        examined = affected_closure(seq, op.line, packs, edges)
        hold_list = crud.build_hold_list(packs)
        db.commit()
    except Exception:
        db.rollback()  # previous cleaning result and hold verdicts stay in force
        raise HTTPException(500, "correction failed; previous cleaning result retained")

    return CorrectCleanOut(
        seq=seq,
        line=op.line,
        previous_passed=previous_passed,
        new_passed=bool(op.passed),
        changed=changed,
        examined_batch_nos=examined,
        examined_count=len(examined),
        hold_list=hold_list,
        message=("conclusion unchanged" if not changed
                 else f"cleaning corrected; {len(examined)} batches re-examined, hold list regenerated"),
    )


# ---------------- status ----------------
@router.get("/status", response_model=StatusOut, tags=["status"])
def status(db: Session = Depends(get_db)):
    ops = list(db.scalars(select(Operation)).all())
    lines = {}
    for line in ("A", "B"):
        line_ops = [o for o in ops if o.line == line]
        cleans = [o for o in line_ops if o.op_type == "clean"]
        lines[line] = {
            "operation_count": len(line_ops),
            "last_clean_passed": cleans[-1].passed if cleans else None,
        }
    ingredient_count = db.scalar(select(func.count()).select_from(IngredientBatch)) or 0
    return StatusOut(
        operation_count=len(ops),
        ingredient_count=ingredient_count,
        lines=lines,
    )
