"""Service layer: load DB rows into the engine, recompute and persist."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import engine as eng
from .models import IngredientBatch, Operation, PackRef
from .schemas import HoldItem, HoldListOut


def _load_ingredients(db: Session) -> dict[str, list[str]]:
    rows = db.scalars(select(IngredientBatch)).all()
    return {r.batch_no: list(r.allergens or []) for r in rows}


def _load_operations(db: Session) -> tuple[list[eng.PackData], list[eng.CleanData], dict[str, Operation]]:
    ops = list(db.scalars(select(Operation).order_by(Operation.seq)).all())
    packs: list[eng.PackData] = []
    cleans: list[eng.CleanData] = []
    op_by_product: dict[str, Operation] = {}
    for op in ops:
        if op.op_type == "pack":
            refs = [(r.kind, r.ref_no) for r in op.refs]
            packs.append(eng.PackData(
                seq=op.seq,
                line=op.line,
                product_batch_no=op.product_batch_no,  # type: ignore[arg-type]
                refs=refs,
                label_allergens=list(op.label_allergens or []),
            ))
            op_by_product[op.product_batch_no] = op  # type: ignore[index]
        else:
            cleans.append(eng.CleanData(seq=op.seq, line=op.line, passed=bool(op.passed)))
    return packs, cleans, op_by_product


def recompute_and_persist(db: Session) -> list[eng.PackData]:
    """Full-shift recomputation; write results back inside the caller's tx."""
    ingredients = _load_ingredients(db)
    packs, cleans, op_by_product = _load_operations(db)

    eng.propagate(ingredients, packs, cleans)

    for pack in packs:
        op = op_by_product[pack.product_batch_no]
        op.actual_allergens = sorted(pack.actual_allergens)
        op.actual_paths = sorted(list(p) for p in pack.actual_paths)
        op.on_hold = pack.on_hold
    db.flush()
    return packs


def build_hold_list(packs: list[eng.PackData]) -> HoldListOut:
    items: list[HoldItem] = []
    for pack in sorted(packs, key=lambda p: p.seq):
        if not pack.on_hold:
            continue
        items.append(HoldItem(
            product_batch_no=pack.product_batch_no,
            seq=pack.seq,
            line=pack.line,
            missing_on_label=pack.missing_on_label,
            missing_fields=pack.missing_fields,
            trace_paths=pack.trace_paths,
            unknown_refs=pack.unknown_refs,
        ))
    return HoldListOut(
        generated=True,
        items=items,
        held_batch_count=len(items),
    )


def get_rework_edges(packs: list[eng.PackData]) -> dict[str, list[str]]:
    return eng.build_rework_edges(packs)


def next_seq(db: Session) -> int:
    max_seq = db.scalar(select(Operation.seq).order_by(Operation.seq.desc()).limit(1))
    return (max_seq or 0) + 1


def add_pack(db: Session, data) -> Operation:
    op = Operation(
        seq=data.seq,
        line=data.line,
        op_type="pack",
        product_batch_no=data.product_batch_no,
        label_allergens=data.label_allergens,
        actual_allergens=[],
        actual_paths=[],
        on_hold=False,
    )
    op.refs = [PackRef(kind=r.kind, ref_no=r.ref_no) for r in data.refs]
    db.add(op)
    return op


def add_clean(db: Session, data) -> Operation:
    op = Operation(
        seq=data.seq,
        line=data.line,
        op_type="clean",
        passed=data.passed,
    )
    db.add(op)
    return op


def serialize_operation(op: Operation) -> dict:
    return {
        "id": op.id,
        "seq": op.seq,
        "line": op.line,
        "op_type": op.op_type,
        "product_batch_no": op.product_batch_no,
        "label_allergens": op.label_allergens,
        "actual_allergens": op.actual_allergens,
        "actual_paths": op.actual_paths,
        "on_hold": op.on_hold,
        "passed": op.passed,
        "refs": [{"kind": r.kind, "ref_no": r.ref_no} for r in op.refs],
    }
