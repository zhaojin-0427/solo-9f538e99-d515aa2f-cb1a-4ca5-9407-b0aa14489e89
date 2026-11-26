"""Pydantic request / response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

LINE_A = "A"
LINE_B = "B"
LINES = (LINE_A, LINE_B)


# ---------- ingredient ----------
class IngredientIn(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=128)
    allergens: list[str] = Field(default_factory=list)

    @field_validator("allergens")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        return sorted({a.strip() for a in v if a.strip()})


class IngredientOut(IngredientIn):
    id: int


# ---------- refs ----------
class PackRefIn(BaseModel):
    kind: Literal["ingredient", "rework"]
    ref_no: str = Field(min_length=1, max_length=64)


# ---------- operations ----------
class PackIn(BaseModel):
    seq: int = Field(ge=1)
    line: Literal["A", "B"]
    product_batch_no: str = Field(min_length=1, max_length=64)
    refs: list[PackRefIn] = Field(default_factory=list)
    label_allergens: list[str] = Field(default_factory=list)

    @field_validator("label_allergens")
    @classmethod
    def _dedup_label(cls, v: list[str]) -> list[str]:
        return sorted({a.strip() for a in v if a.strip()})


class CleanIn(BaseModel):
    seq: int = Field(ge=1)
    line: Literal["A", "B"]
    passed: bool


class OperationOut(BaseModel):
    id: int
    seq: int
    line: str
    op_type: str
    product_batch_no: str | None
    label_allergens: list[str] | None
    actual_allergens: list[str] | None
    actual_paths: list[list[str]] | None
    on_hold: bool | None
    passed: bool | None
    refs: list[PackRefIn]


# ---------- hold list ----------
class HoldItem(BaseModel):
    product_batch_no: str
    seq: int
    line: str
    missing_on_label: list[str]          # 漏标：实际含有但标签未印的过敏原
    missing_fields: list[str]            # 缺项：批号 / 用料 / 标签过敏原未填
    trace_paths: list[list[str]]         # 每个漏标过敏原追到原料的路径
    unknown_refs: list[str]              # 引用了不存在的用料/成品批号


class HoldListOut(BaseModel):
    generated: bool
    items: list[HoldItem]
    held_batch_count: int


# ---------- correction ----------
class CorrectCleanIn(BaseModel):
    passed: bool


class CorrectCleanOut(BaseModel):
    seq: int
    line: str
    previous_passed: bool
    new_passed: bool
    changed: bool
    examined_batch_nos: list[str]        # 实查批号：波及范围内真正复核到的成品批
    examined_count: int
    hold_list: HoldListOut
    message: str


class StatusOut(BaseModel):
    operation_count: int
    ingredient_count: int
    lines: dict[str, dict]
