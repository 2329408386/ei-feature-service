#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""计算批次：创建补算 / 重算、查询、取消。实际执行在 src/engine/batch_runner.py。"""

from sqlalchemy import func, select

from src.api.schemas import BatchIn
from src.model.tables import FeatureBatch
from src.service.binding import active, features_of
from src.util import db
from src.util.config import BATCH_MAX_DAYS
from src.util.errors import invalid, invalid_state, not_found
from src.util.timeutil import day_range, now, today


async def create(body: BatchIn, user: str) -> FeatureBatch:
    """校验日期范围和特征（须在腔室生效修订中）后创建排队中的补算 / 重算批次。"""
    chamber, features, start, end = body.chamber, body.features, body.from_, body.to
    errors = []
    if start > end:
        errors.append({"path": "from", "message": "开始日期不能晚于结束日期"})
    if end > today():
        errors.append({"path": "to", "message": "结束日期不能晚于今天"})
    if (end - start).days + 1 > BATCH_MAX_DAYS:
        errors.append({"path": "to", "message": f"单次最多 {BATCH_MAX_DAYS} 天"})
    async with db.session() as s:
        a = await active(s, chamber)
        if a is None:
            raise invalid_state(f"腔室 {chamber} 没有生效的绑定", "none")
        missing = sorted(set(features) - features_of(a.spec))
        if missing:
            errors.append({"path": "features", "message": f"不在生效修订 {a.revision} 中：{'、'.join(missing)}"})
        if errors:
            raise invalid("VALIDATION_ERROR", "参数错误", errors)
        data_from, data_to = day_range(start, end)
        row = FeatureBatch(
            mode=body.mode, status="queued", chamber=chamber, binding_revision=a.revision,
            features=sorted(set(features)), data_from=data_from, data_to=data_to, reason=body.reason, created_by=user,
        )
        s.add(row)
    return row


async def list_batches(chamber: str | None, mode: str | None, status: str | None, page: int, size: int):
    """分页查询批次，新的在前。返回 (rows, total)。"""
    cond = []
    if chamber:
        cond.append(FeatureBatch.chamber == chamber)
    if mode:
        cond.append(FeatureBatch.mode == mode)
    if status:
        cond.append(FeatureBatch.status == status)
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(FeatureBatch).where(*cond))
        stmt = (
            select(FeatureBatch).where(*cond).order_by(FeatureBatch.batch_id.desc())
            .offset((page - 1) * size).limit(size)
        )
        return (await s.execute(stmt)).scalars().all(), total


async def get(batch_id: int) -> FeatureBatch:
    """查询单个批次，不存在返回 404。"""
    async with db.session() as s:
        row = await s.get(FeatureBatch, batch_id)
    if row is None:
        raise not_found(f"批次 {batch_id}")
    return row


async def cancel(batch_id: int) -> FeatureBatch:
    """取消排队中或进行中的补算 / 重算；执行器在处理下一个 run_id 前发现后停止，已写入的值保留。"""
    async with db.session() as s:
        row = await s.get(FeatureBatch, batch_id, with_for_update=True)
        if row is None:
            raise not_found(f"批次 {batch_id}")
        if row.mode == "live" or row.status not in ("queued", "running"):
            raise invalid_state("只有排队中或进行中的补算、重算可以取消", row.status)
        row.status, row.finished_at = "cancelled", now()
    return row
