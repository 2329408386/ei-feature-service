#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""ORM 映射。表结构以 schema.sql 为准，这里只做映射，不用于建表。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict: JSONB, list: JSONB}


def op_ref(feature_id: str, operator: str, version: int) -> str:
    """算子引用：feature_id/operator@version。"""
    return f"{feature_id}/{operator}@{version}"


class _Reviewed:
    """特征和算子共用的评审 / 下线字段。"""

    status: Mapped[str]  # draft / review / published / retired
    spec: Mapped[dict]
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    submitted_at: Mapped[datetime | None]
    published_by: Mapped[str | None]
    published_at: Mapped[datetime | None]
    retired_by: Mapped[str | None]
    retired_at: Mapped[datetime | None]
    retire_reason: Mapped[str | None]
    review_log: Mapped[list] = mapped_column(default=list)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Feature(_Reviewed, Base):
    __tablename__ = "feature"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)

    @property
    def ref(self) -> str:
        """形如 id@version。"""
        return f"{self.feature_id}@{self.version}"


class Operator(_Reviewed, Base):
    __tablename__ = "operator"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_file: Mapped[str]
    code_uri: Mapped[str]
    code_hash: Mapped[str]

    @property
    def ref(self) -> str:
        """形如 feature_id/name@version。"""
        return op_ref(self.feature_id, self.name, self.version)


class OperatorBinding(Base):
    __tablename__ = "operator_binding"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int]
    spec: Mapped[dict]
    status: Mapped[str]  # pending / active / rejected / withdrawn / superseded
    source: Mapped[str]  # console / api / import / system
    base_revision: Mapped[int | None]
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reviewed_by: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    review_note: Mapped[str | None]
    superseded_at: Mapped[datetime | None]


class BindingItem(Base):
    __tablename__ = "binding_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feature_id", "operator", "revision"],
            ["operator_binding.feature_id", "operator_binding.operator", "operator_binding.revision"],
        ),
    )

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe: Mapped[str] = mapped_column(Text, primary_key=True)
    chamber: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str]
    version: Mapped[int]


class ChamberSubscription(Base):
    __tablename__ = "chamber_subscription"

    chamber: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str]
    recipes: Mapped[list] = mapped_column(default=list)
    gde_sub_id: Mapped[str | None]
    synced_recipes: Mapped[list | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    synced_at: Mapped[datetime | None]


class FeatureBatch(Base):
    __tablename__ = "feature_batch"

    batch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str]  # live / fill / overwrite
    status: Mapped[str]  # queued / running / done / failed / cancelled
    chamber: Mapped[str]
    operators: Mapped[list | None]
    plan: Mapped[dict | None]
    data_from: Mapped[datetime]
    data_to: Mapped[datetime]
    reason: Mapped[str | None]
    superseded_by: Mapped[int | None] = mapped_column(BigInteger)
    run_total: Mapped[int] = mapped_column(default=0)
    value_ok: Mapped[int] = mapped_column(default=0)
    value_null: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class FeatureValue(Base):
    __tablename__ = "feature_value"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str]
    chamber: Mapped[str]
    recipe: Mapped[str]
    lot: Mapped[str | None]
    wafer: Mapped[str | None]
    run_end_at: Mapped[datetime]
    value: Mapped[float | None]
    reason: Mapped[str | None]
    reason_detail: Mapped[str | None]
    batch_id: Mapped[int] = mapped_column(BigInteger)
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())
