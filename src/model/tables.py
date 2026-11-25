#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""ORM 映射。表结构以 schema.sql 为准，这里只做映射，不用于建表。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, Integer, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict: JSONB, list: JSONB}


class Feature(Base):
    __tablename__ = "feature"

    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str]  # draft / review / published / retired
    spec: Mapped[dict]
    package_uri: Mapped[str]
    package_hash: Mapped[str]
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

    @property
    def ref(self) -> str:
        """形如 id@version。"""
        return f"{self.feature_id}@{self.version}"


class ChamberBinding(Base):
    __tablename__ = "chamber_binding"

    chamber: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    tool: Mapped[str]
    spec: Mapped[dict]
    status: Mapped[str]  # pending / active / rejected / withdrawn / superseded
    source: Mapped[str]  # console / api / import / system
    base_revision: Mapped[int | None]
    gde_sub_id: Mapped[str | None]
    created_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    reviewed_by: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    review_note: Mapped[str | None]
    superseded_at: Mapped[datetime | None]


class BindingItem(Base):
    __tablename__ = "binding_item"
    __table_args__ = (
        ForeignKeyConstraint(["chamber", "revision"], ["chamber_binding.chamber", "chamber_binding.revision"]),
    )

    chamber: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rule_no: Mapped[int] = mapped_column(SmallInteger)
    version: Mapped[str]


class FeatureBatch(Base):
    __tablename__ = "feature_batch"

    batch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str]  # live / fill / overwrite
    status: Mapped[str]  # queued / running / done / failed / cancelled
    chamber: Mapped[str]
    binding_revision: Mapped[int]
    features: Mapped[list | None]
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
    version: Mapped[str] = mapped_column(Text, primary_key=True)
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


class GdeOutbox(Base):
    __tablename__ = "gde_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action: Mapped[str]  # update_subscription / cancel_subscription
    payload: Mapped[dict]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    done_at: Mapped[datetime | None]
