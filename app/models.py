"""ORM models: ingredient batches, operations (packing/cleaning) and their links."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class IngredientBatch(Base):
    __tablename__ = "ingredient_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    allergens: Mapped[list] = mapped_column(JSON, default=list)  # ["egg", ...]


class Operation(Base):
    """One operation on one packing line, ordered by the shift-wide seq."""

    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("seq", name="uq_operation_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(index=True)
    line: Mapped[str] = mapped_column(String(16), index=True)  # "A" / "B"
    op_type: Mapped[str] = mapped_column(String(16))  # "pack" / "clean"

    # --- packing fields (NULL for cleaning) ---
    product_batch_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    label_allergens: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # allergens actually carried, computed by the propagation engine
    actual_allergens: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # provenance paths actually carried, computed by the propagation engine
    actual_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    on_hold: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- cleaning fields (NULL for packing) ---
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    refs: Mapped[list["PackRef"]] = relationship(
        back_populates="operation", cascade="all, delete-orphan", order_by="PackRef.id"
    )


class PackRef(Base):
    """An input of a packing: either an ingredient batch or a reworked product batch.

    kind = "ingredient" -> ref_no points at IngredientBatch.batch_no
    kind = "rework"      -> ref_no points at an earlier Operation.product_batch_no
    """

    __tablename__ = "pack_refs"
    __table_args__ = (UniqueConstraint("operation_id", "kind", "ref_no", name="uq_pack_ref"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[int] = mapped_column(ForeignKey("operations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_no: Mapped[str] = mapped_column(String(64), index=True)

    operation: Mapped[Operation] = relationship(back_populates="refs")
