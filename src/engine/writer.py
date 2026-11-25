#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""写特征值、维护批次。实时和补算 / 重算共用。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from src.engine.sandbox import RunResult
from src.model.tables import BindingItem, ChamberBinding, Feature, FeatureBatch, FeatureValue
from src.util.auth import SYSTEM
from src.util.timeutil import hour_floor, now


@dataclass(frozen=True)
class RunRef:
    """写特征值需要的执行信息（来自 Redis 通知或 GDE 执行清单）。"""

    run_id: str
    tool: str
    chamber: str
    recipe: str
    lot: str | None
    wafer: str | None
    end_at: datetime


@dataclass(frozen=True)
class FeatureRef:
    feature_id: str
    version: str
    package_hash: str

    @property
    def ref(self) -> str:
        """形如 id@version。"""
        return f"{self.feature_id}@{self.version}"


async def active_revision(s, chamber: str) -> int | None:
    """腔室生效修订号，没有生效绑定返回 None。"""
    stmt = select(ChamberBinding.revision).where(ChamberBinding.chamber == chamber, ChamberBinding.status == "active")
    return await s.scalar(stmt)


async def features_for(s, chamber: str, revision: int, recipe: str) -> list[FeatureRef]:
    """某修订中，该配方要计算的特征（带特征包 hash）。"""
    stmt = (
        select(Feature.feature_id, Feature.version, Feature.package_hash)
        .join(BindingItem, (BindingItem.feature_id == Feature.feature_id) & (BindingItem.version == Feature.version))
        .where(BindingItem.chamber == chamber, BindingItem.revision == revision, BindingItem.recipe == recipe)
        .order_by(Feature.feature_id)
    )
    return [FeatureRef(*row) for row in (await s.execute(stmt)).tuples().all()]


async def live_batch(s, chamber: str, revision: int, end_at: datetime) -> int:
    """取或建实时批次：键 (腔室, 修订, 执行结束时间所在整点)。"""
    start = hour_floor(end_at)
    await s.execute(
        insert(FeatureBatch)
        .values(mode="live", status="running", chamber=chamber, binding_revision=revision, data_from=start,
                data_to=start + timedelta(hours=1), created_by=SYSTEM, started_at=now())
        .on_conflict_do_nothing(
            index_elements=["chamber", "binding_revision", "data_from"], index_where=FeatureBatch.mode == "live"
        )
    )
    stmt = select(FeatureBatch.batch_id).where(
        FeatureBatch.mode == "live", FeatureBatch.chamber == chamber,
        FeatureBatch.binding_revision == revision, FeatureBatch.data_from == start,
    )
    return await s.scalar(stmt)


async def has_value(s, f: FeatureRef, run_id: str) -> bool:
    """该特征版本在该执行上是否已有结果（含空值）。"""
    stmt = select(FeatureValue.run_id).where(
        FeatureValue.feature_id == f.feature_id, FeatureValue.version == f.version, FeatureValue.run_id == run_id
    )
    return await s.scalar(stmt) is not None


async def write_value(s, batch_id: int, f: FeatureRef, run: RunRef, r: RunResult, overwrite: bool = False) -> bool:
    """写一个特征值并更新批次计数。overwrite=True 时先删后插（同一事务）。返回是否写入。"""
    key = (
        FeatureValue.feature_id == f.feature_id, FeatureValue.version == f.version, FeatureValue.run_id == run.run_id
    )
    if overwrite:
        await s.execute(delete(FeatureValue).where(*key))
    inserted = await s.scalar(
        insert(FeatureValue).values(
            feature_id=f.feature_id, version=f.version, run_id=run.run_id, tool=run.tool, chamber=run.chamber,
            recipe=run.recipe, lot=run.lot, wafer=run.wafer, run_end_at=run.end_at, value=r.value,
            reason=r.reason, reason_detail=r.reason_detail, batch_id=batch_id,
        ).on_conflict_do_nothing().returning(FeatureValue.run_id)
    )
    if inserted is None:  # 已有结果（重复通知 / fill 并发），不计数
        return False
    counter = FeatureBatch.value_ok if r.value is not None else FeatureBatch.value_null
    await s.execute(update(FeatureBatch).where(FeatureBatch.batch_id == batch_id).values({counter: counter + 1}))
    return True


async def add_run(s, batch_id: int) -> None:
    """批次已处理的执行数 +1。"""
    stmt = update(FeatureBatch).where(FeatureBatch.batch_id == batch_id)
    await s.execute(stmt.values(run_total=FeatureBatch.run_total + 1))
