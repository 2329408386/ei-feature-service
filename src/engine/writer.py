#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""写特征值、维护批次。实时和补算 / 重算共用。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from src.engine.sandbox import OperatorRef, RunResult
from src.model.tables import BindingItem, FeatureBatch, FeatureValue, Operator, OperatorBinding
from src.util.auth import SYSTEM
from src.util.timeutil import hour_floor, now

__all__ = [
    "OperatorRef", "RunRef", "add_run", "has_value", "live_batch", "load_operators", "operators_for",
    "overwrite_value", "write_value",
]


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


def _op_select():
    return select(Operator.feature_id, Operator.name, Operator.version, Operator.code_hash, Operator.code_file)


async def operators_for(s, chamber: str, recipe: str) -> list[OperatorRef]:
    """生效绑定中，该腔室的该 recipe 要计算的算子版本。"""
    stmt = (
        _op_select()
        .join(BindingItem, (BindingItem.feature_id == Operator.feature_id) & (BindingItem.operator == Operator.name)
              & (BindingItem.version == Operator.version))
        .join(OperatorBinding, (OperatorBinding.feature_id == BindingItem.feature_id)
              & (OperatorBinding.operator == BindingItem.operator) & (OperatorBinding.revision == BindingItem.revision))
        .where(OperatorBinding.status == "active", BindingItem.chamber == chamber, BindingItem.recipe == recipe)
        .order_by(Operator.feature_id, Operator.name)
    )
    return [OperatorRef(*row) for row in (await s.execute(stmt)).tuples().all()]


async def load_operators(s, keys: list[tuple[str, str, int]]) -> dict[str, OperatorRef]:
    """按 (feature_id, operator, version) 读算子，返回 {引用: OperatorRef}。"""
    if not keys:
        return {}
    stmt = _op_select().where(tuple_(Operator.feature_id, Operator.name, Operator.version).in_(keys))
    refs = [OperatorRef(*row) for row in (await s.execute(stmt)).tuples().all()]
    return {r.ref: r for r in refs}


async def live_batch(s, chamber: str, end_at: datetime) -> int:
    """取或建实时批次：键 (腔室, 执行结束时间所在整点)。"""
    start = hour_floor(end_at)
    await s.execute(
        insert(FeatureBatch)
        .values(mode="live", status="running", chamber=chamber, data_from=start,
                data_to=start + timedelta(hours=1), created_by=SYSTEM, started_at=now())
        .on_conflict_do_nothing(index_elements=["chamber", "data_from"], index_where=FeatureBatch.mode == "live")
    )
    stmt = select(FeatureBatch.batch_id).where(
        FeatureBatch.mode == "live", FeatureBatch.chamber == chamber, FeatureBatch.data_from == start,
    )
    return await s.scalar(stmt)


def _value_key(op: OperatorRef, run_id: str) -> list:
    return [FeatureValue.feature_id == op.feature_id, FeatureValue.operator == op.operator,
            FeatureValue.version == op.version, FeatureValue.run_id == run_id]


async def has_value(s, op: OperatorRef, run_id: str) -> bool:
    """该算子版本在该执行上是否已有结果（含空值）。"""
    return await s.scalar(select(FeatureValue.run_id).where(*_value_key(op, run_id))) is not None


async def overwrite_value(s, batch_id: int, op: OperatorRef, run: RunRef, r: RunResult) -> bool:
    """重算：同一事务里先删除旧值，再按 write_value 写入。返回是否写入。"""
    await s.execute(delete(FeatureValue).where(*_value_key(op, run.run_id)))
    return await write_value(s, batch_id, op, run, r)


async def write_value(s, batch_id: int, op: OperatorRef, run: RunRef, r: RunResult) -> bool:
    """写一个特征值并更新批次计数。已有结果时不写、不计数。返回是否写入。"""
    inserted = await s.scalar(
        insert(FeatureValue).values(
            feature_id=op.feature_id, operator=op.operator, version=op.version, run_id=run.run_id, tool=run.tool,
            chamber=run.chamber, recipe=run.recipe, lot=run.lot, wafer=run.wafer, run_end_at=run.end_at,
            value=r.value, reason=r.reason, reason_detail=r.reason_detail, batch_id=batch_id,
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
