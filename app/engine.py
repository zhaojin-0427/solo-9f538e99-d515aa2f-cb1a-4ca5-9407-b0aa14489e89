"""Pure propagation engine.

Rules implemented
-----------------
* Shift-wide ``seq`` orders every operation of both packing lines.
* A packing's actual allergens come from three sources:
    1. ingredient batches used directly;
    2. earlier finished products reworked into this batch (any line);
    3. residue left on this packing line by earlier batches.
* After packing, the batch leaves every allergen it carries on its own line.
* A passed cleaning clears the line residue; a failed cleaning keeps it.
* Cleaning never changes finished products: their allergens stay available to
  later rework, including across lines.
* Every allergen carries a trace path whose last two nodes are the root
  ingredient batch and the allergen name, e.g.
  ``["P3", "rework:P2", "residue:A", "ING1", "egg"]``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

INGREDIENT_HOP = "ingredient"
REWORK_HOP = "rework"
RESIDUE_HOP = "residue"


@dataclass
class PackData:
    seq: int
    line: str
    product_batch_no: str
    refs: list[tuple[str, str]]          # (kind, ref_no)
    label_allergens: list[str]
    # filled by the engine
    actual_allergens: set[str] = field(default_factory=set)
    actual_paths: set[tuple[str, ...]] = field(default_factory=set)
    unknown_refs: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    missing_on_label: list[str] = field(default_factory=list)
    trace_paths: list[list[str]] = field(default_factory=list)
    on_hold: bool = False


@dataclass
class CleanData:
    seq: int
    line: str
    passed: bool


def _extend(path: tuple[str, ...], product: str, hop: str) -> tuple[str, ...]:
    """Prepend this product and a hop marker to an inherited path."""
    return (product, f"{hop}:{path[0]}") + path[1:]


def propagate(
    ingredients: dict[str, list[str]],
    packs: list[PackData],
    cleans: list[CleanData],
) -> None:
    """Run the shift in seq order; mutate ``packs`` with computed results."""
    # residue[line][allergen] = set of path tuples deposited by earlier packs
    residue: dict[str, dict[str, set[tuple[str, ...]]]] = {"A": {}, "B": {}}
    # product batch -> its computed result, so later rework can look it up
    products: dict[str, PackData] = {}

    timeline: list[tuple[int, str, object]] = [
        (p.seq, "pack", p) for p in packs
    ] + [(c.seq, "clean", c) for c in cleans]
    timeline.sort(key=lambda x: x[0])

    for _, kind, op in timeline:
        if kind == "clean":
            assert isinstance(op, CleanData)
            if op.passed:
                residue[op.line] = {}
            # failed cleaning: residue is untouched
            continue

        pack = op
        assert isinstance(pack, PackData)
        paths: set[tuple[str, ...]] = set()
        allergens: set[str] = set()
        unknown: list[str] = []

        # 1 & 2: direct ingredients and reworked products
        for ref_kind, ref_no in pack.refs:
            if ref_kind == "ingredient":
                ing = ingredients.get(ref_no)
                if ing is None:
                    unknown.append(f"ingredient:{ref_no}")
                    continue
                for allergen in ing:
                    paths.add((pack.product_batch_no,
                               f"{INGREDIENT_HOP}:{ref_no}", allergen))
                    allergens.add(allergen)
            else:  # rework
                src = products.get(ref_no)
                if src is None:
                    unknown.append(f"rework:{ref_no}")
                    continue
                for src_path in src.actual_paths:
                    paths.add(_extend(src_path, pack.product_batch_no, REWORK_HOP))
                allergens.update(src.actual_allergens)

        # 3: residue on this line (start of shift => empty, nothing to do)
        for allergen, r_paths in residue[pack.line].items():
            for r_path in r_paths:
                paths.add(_extend(r_path, pack.product_batch_no, RESIDUE_HOP))
            allergens.add(allergen)

        pack.actual_allergens = allergens
        pack.actual_paths = paths
        pack.unknown_refs = sorted(set(unknown))

        # The batch leaves every allergen it carries on its own line.
        for allergen in allergens:
            bucket = residue[pack.line].setdefault(allergen, set())
            bucket.update(p for p in paths if p[-1] == allergen)

        products[pack.product_batch_no] = pack

    _evaluate_defects(packs)


def _evaluate_defects(packs: list[PackData]) -> None:
    for pack in packs:
        missing_fields: list[str] = []
        if not pack.refs:
            missing_fields.append("materials")          # 用料批号缺项
        if not pack.label_allergens:
            missing_fields.append("label_allergens")   # 标签过敏原缺项
        pack.missing_fields = missing_fields

        label_set = set(pack.label_allergens)
        pack.missing_on_label = sorted(
            a for a in pack.actual_allergens if a not in label_set
        )
        pack.trace_paths = sorted(
            list(p) for p in pack.actual_paths if p[-1] in pack.missing_on_label
        )
        pack.on_hold = bool(
            pack.missing_on_label or pack.missing_fields or pack.unknown_refs
        )


def affected_closure(
    clean_seq: int,
    clean_line: str,
    packs: list[PackData],
    rework_edges: dict[str, list[str]],
) -> list[str]:
    """Batches a cleaning correction can actually reach.

    Start: later packs on the cleaned line (residue chain). Then expand through
    rework edges, which may cross to the other line and propagate transitively.
    ``rework_edges[src_product]`` = list of product batch nos that rework it.
    """
    by_product = {p.product_batch_no: p for p in packs}
    reached: set[str] = set()
    queue: list[str] = []
    for pack in packs:
        if pack.line == clean_line and pack.seq > clean_seq:
            reached.add(pack.product_batch_no)
            queue.append(pack.product_batch_no)

    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        for downstream in rework_edges.get(current, []):
            if downstream not in reached and downstream in by_product:
                reached.add(downstream)
                queue.append(downstream)

    # report in shift order for stable output
    return [p.product_batch_no for p in sorted(packs, key=lambda x: x.seq)
            if p.product_batch_no in reached]


def build_rework_edges(packs: list[PackData]) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for pack in packs:
        for kind, ref_no in pack.refs:
            if kind == "rework":
                edges.setdefault(ref_no, []).append(pack.product_batch_no)
    return edges
