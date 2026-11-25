#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""腔室订阅：GDE 订阅按腔室，订阅范围 = 所有生效算子绑定在该腔室上的 recipe 并集。

- refresh(s, chambers)：在调用方事务里按 binding_item 重算期望范围，写入 chamber_subscription.recipes；
- sync(chambers)：事务提交后把期望范围同步到 GDE（新建 / 修改 / 取消订阅）；失败记录在行上，
  由后台任务 src/engine/jobs.py 每分钟对所有未同步的腔室重试。
"""

import asyncio
import logging

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from src.model.tables import BindingItem, ChamberSubscription, OperatorBinding
from src.util import db
from src.util.gde import get_gde
from src.util.timeutil import now

log = logging.getLogger(__name__)


async def refresh(s, chambers: set[str]) -> None:
    """重算这些腔室的期望订阅范围（按腔室名排序加锁，避免死锁）。"""
    for chamber in sorted(chambers):
        await db.lock(s, f"sub:{chamber}")
        rows = (await s.execute(
            select(BindingItem.tool, BindingItem.recipe).distinct().join(
                OperatorBinding,
                (OperatorBinding.feature_id == BindingItem.feature_id)
                & (OperatorBinding.operator == BindingItem.operator)
                & (OperatorBinding.revision == BindingItem.revision),
            ).where(OperatorBinding.status == "active", BindingItem.chamber == chamber)
        )).tuples().all()
        recipes = sorted({r for _, r in rows})
        tool = rows[0][0] if rows else None
        stmt = insert(ChamberSubscription).values(chamber=chamber, tool=tool or "", recipes=recipes)
        update = {"recipes": recipes, "updated_at": now()}
        if tool:
            update["tool"] = tool
        if tool:  # 新腔室插入一行；已有的行（包括变空的）只更新范围
            await s.execute(stmt.on_conflict_do_update(index_elements=["chamber"], set_=update))
        else:
            await s.execute(
                ChamberSubscription.__table__.update()
                .where(ChamberSubscription.chamber == chamber).values(**update)
            )


def _needs_sync(row: ChamberSubscription) -> bool:
    return row.recipes != row.synced_recipes or bool(row.recipes) != bool(row.gde_sub_id)


async def _sync_one(chamber: str) -> None:
    """把一个腔室的期望范围同步到 GDE。行被其他实例锁住时跳过（对方在同步）。"""
    gde = get_gde()
    async with db.session() as s:
        stmt = select(ChamberSubscription).where(ChamberSubscription.chamber == chamber)
        row = (await s.execute(stmt.with_for_update(skip_locked=True))).scalar_one_or_none()
        if row is None or not _needs_sync(row):
            return
        want, sub_id = list(row.recipes), row.gde_sub_id
        try:
            if want and not sub_id:
                sub_id = await asyncio.to_thread(gde.subscribe, row.tool, chamber, want)
            elif want:
                await asyncio.to_thread(gde.update_subscription, sub_id, want)
            elif sub_id:
                await asyncio.to_thread(gde.cancel_subscription, sub_id)
                sub_id = None
        except Exception as e:
            row.attempts, row.last_error = row.attempts + 1, repr(e)[:1024]
            log.warning("腔室 %s 订阅同步失败（第 %s 次）：%s", chamber, row.attempts, e)
            return
        row.gde_sub_id, row.synced_recipes, row.synced_at = sub_id, want, now()
        row.attempts, row.last_error = 0, None


async def sync(chambers: set[str] | None = None) -> None:
    """同步指定腔室；不指定时同步全部未同步的腔室。单个腔室失败不影响其他腔室。"""
    if chambers is None:
        async with db.session() as s:
            chambers = set((await s.execute(
                select(ChamberSubscription.chamber).where(or_(
                    ChamberSubscription.synced_recipes.is_(None),
                    ChamberSubscription.recipes != ChamberSubscription.synced_recipes,
                    (ChamberSubscription.recipes != []) & ChamberSubscription.gde_sub_id.is_(None),
                ))
            )).scalars().all())
    for chamber in sorted(chambers):
        try:
            await _sync_one(chamber)
        except Exception:
            log.exception("腔室 %s 订阅同步出错", chamber)
