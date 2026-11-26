#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""recipe 订阅：GDE 按 recipe 订阅，订阅后自动推送该 recipe 在所有机台 / 腔室上的执行（含以后新增的腔室）。

- refresh(s, recipes)：在调用方事务里按 binding_item 重算这些 recipe 是否需要订阅（wanted）；
- sync(recipes)：事务提交后把 wanted 同步到 GDE（新建 / 取消订阅）；失败记录在行上，
  由后台任务 src/engine/jobs.py 每分钟对所有未同步的 recipe 重试。
"""

import asyncio
import logging

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from src.model.tables import BindingItem, OperatorBinding, RecipeSubscription
from src.util import db
from src.util.gde import get_gde
from src.util.timeutil import now

log = logging.getLogger(__name__)


def active_items(*cols):
    """生效修订的 binding_item 上查询 cols（调用方再加条件）。"""
    return select(*cols).select_from(BindingItem).join(
        OperatorBinding,
        (OperatorBinding.feature_id == BindingItem.feature_id) & (OperatorBinding.operator == BindingItem.operator)
        & (OperatorBinding.revision == BindingItem.revision),
    ).where(OperatorBinding.status == "active")


async def refresh(s, recipes: set[str]) -> None:
    """重算这些 recipe 是否需要订阅（按名称排序加锁，避免死锁）。"""
    for recipe in sorted(recipes):
        await db.lock(s, f"sub:{recipe}")
        stmt = active_items(BindingItem.recipe).where(BindingItem.recipe == recipe).limit(1)
        wanted = await s.scalar(stmt) is not None
        await s.execute(
            insert(RecipeSubscription).values(recipe=recipe, wanted=wanted)
            .on_conflict_do_update(index_elements=["recipe"], set_={"wanted": wanted, "updated_at": now()})
        )


def _needs_sync(row: RecipeSubscription) -> bool:
    return row.wanted != bool(row.gde_sub_id)


async def _sync_one(recipe: str) -> None:
    """把一个 recipe 的订阅同步到 GDE。行被其他实例锁住时跳过（对方在同步）。"""
    gde = get_gde()
    async with db.session() as s:
        stmt = select(RecipeSubscription).where(RecipeSubscription.recipe == recipe)
        row = (await s.execute(stmt.with_for_update(skip_locked=True))).scalar_one_or_none()
        if row is None or not _needs_sync(row):
            return
        try:
            if row.wanted:
                sub_id = await asyncio.to_thread(gde.subscribe, recipe)
            else:
                await asyncio.to_thread(gde.cancel_subscription, row.gde_sub_id)
                sub_id = None
        except Exception as e:
            row.attempts, row.last_error = row.attempts + 1, repr(e)[:1024]
            log.warning("recipe %s 订阅同步失败（第 %s 次）：%s", recipe, row.attempts, e)
            return
        row.gde_sub_id, row.synced_at, row.attempts, row.last_error = sub_id, now(), 0, None


async def sync(recipes: set[str] | None = None) -> None:
    """同步指定 recipe；不指定时同步全部未同步的。单个 recipe 失败不影响其他的。"""
    if recipes is None:
        async with db.session() as s:
            recipes = set((await s.execute(
                select(RecipeSubscription.recipe).where(or_(
                    RecipeSubscription.wanted & RecipeSubscription.gde_sub_id.is_(None),
                    ~RecipeSubscription.wanted & RecipeSubscription.gde_sub_id.is_not(None),
                ))
            )).scalars().all())
    for recipe in sorted(recipes):
        try:
            await _sync_one(recipe)
        except Exception:
            log.exception("recipe %s 订阅同步出错", recipe)
