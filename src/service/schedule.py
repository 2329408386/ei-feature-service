#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""调度：各腔室的订阅状态与处理进度，暂停 / 恢复订阅。订阅状态以 GDE 为准。"""

import asyncio

from sqlalchemy import func, or_, select

from src.model.tables import BindingItem, ChamberSubscription, FeatureBatch, FeatureValue, OperatorBinding
from src.service.binding import _gde
from src.util import db
from src.util.errors import invalid_state, not_found
from src.util.gde import GdeError, get_gde


async def _status(sub_id: str | None) -> str:
    if not sub_id:
        return "cancelled"
    try:
        return await asyncio.to_thread(get_gde().subscription_status, sub_id)
    except GdeError:
        return "unknown"  # 查不到状态不影响列表


async def list_schedules(tool: str | None = None, chamber: str | None = None) -> list[dict]:
    """有订阅范围或仍有 GDE 订阅的腔室：订阅状态（实时查 GDE）、算子数、处理进度。"""
    cond = [or_(ChamberSubscription.recipes != [], ChamberSubscription.gde_sub_id.is_not(None))]
    if tool:
        cond.append(ChamberSubscription.tool == tool)
    if chamber:
        cond.append(ChamberSubscription.chamber == chamber)
    async with db.session() as s:
        rows = (await s.execute(
            select(ChamberSubscription).where(*cond).order_by(ChamberSubscription.chamber)
        )).scalars().all()
        counts = dict((await s.execute(
            select(BindingItem.chamber, func.count(func.distinct(BindingItem.feature_id + "/" + BindingItem.operator)))
            .join(OperatorBinding, (OperatorBinding.feature_id == BindingItem.feature_id)
                  & (OperatorBinding.operator == BindingItem.operator)
                  & (OperatorBinding.revision == BindingItem.revision))
            .where(OperatorBinding.status == "active").group_by(BindingItem.chamber)
        )).tuples().all())
        # 处理进度：实时批次里最新的执行结束时间
        progress = dict((await s.execute(
            select(FeatureValue.chamber, func.max(FeatureValue.run_end_at))
            .join(FeatureBatch, FeatureBatch.batch_id == FeatureValue.batch_id)
            .where(FeatureBatch.mode == "live").group_by(FeatureValue.chamber)
        )).tuples().all())
    statuses = await asyncio.gather(*(_status(r.gde_sub_id) for r in rows))
    return [
        {
            "tool": r.tool, "chamber": r.chamber, "gde_sub_id": r.gde_sub_id, "recipes": r.recipes,
            "operator_count": counts.get(r.chamber, 0), "synced": r.recipes == r.synced_recipes,
            "last_error": r.last_error, "status": st, "progress": progress.get(r.chamber),
        }
        for r, st in zip(rows, statuses)
    ]


async def set_paused(chamber: str, paused: bool) -> None:
    """暂停或恢复腔室的 GDE 订阅。"""
    async with db.session() as s:
        row = await s.get(ChamberSubscription, chamber)
    if row is None:
        raise not_found(f"腔室 {chamber} 的订阅")
    if not row.gde_sub_id:
        raise invalid_state("该腔室没有 GDE 订阅", "cancelled")
    gde = get_gde()
    await _gde(gde.pause_subscription if paused else gde.resume_subscription, row.gde_sub_id)
