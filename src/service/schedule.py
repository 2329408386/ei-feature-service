#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""调度：订阅状态、绑定的算子、跑过该 recipe 的机台 / 腔室和处理进度；暂停 / 恢复订阅。

两种视图：list_schedules 以 recipe 为键；schedule_tree 为 特征 → 算子 → recipe → 机台 / 腔室（调度页使用）。

订阅状态以 GDE 为准；机台 / 腔室由 GDE 按 recipe 反查（GDE 自动推送这些腔室的执行，新增腔室也会出现）。
"""

import asyncio

from sqlalchemy import func, or_, select

from src.model.tables import (
    BindingItem,
    FeatureBatch,
    FeatureMeta,
    FeatureValue,
    OperatorMeta,
    RecipeSubscription,
    op_ref,
)
from src.service.binding import _gde
from src.service.subscription import active_items
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


async def _chambers(recipe: str) -> list[tuple[str, str]] | None:
    try:
        return await asyncio.to_thread(get_gde().recipe_chambers, recipe)
    except GdeError:
        return None  # GDE 不可用或 recipe 已不存在，不影响列表


async def _recipe_operators(s, names: list[str]) -> dict[str, list[str]]:
    """各 recipe 上生效绑定的算子引用。"""
    result = await s.execute(
        active_items(BindingItem.recipe, BindingItem.feature_id, BindingItem.operator, BindingItem.version)
        .where(BindingItem.recipe.in_(names))
    )
    ops: dict[str, list[str]] = {}
    for rcp, fid, op, ver in result.tuples().all():
        ops.setdefault(rcp, []).append(op_ref(fid, op, ver))
    return ops


async def _live_progress(s, names: list[str]) -> dict[tuple[str, str], object]:
    """处理进度：实时批次里各 (recipe, 腔室) 最新的执行结束时间。"""
    result = await s.execute(
        select(FeatureValue.recipe, FeatureValue.chamber, func.max(FeatureValue.run_end_at))
        .join(FeatureBatch, FeatureBatch.batch_id == FeatureValue.batch_id)
        .where(FeatureBatch.mode == "live", FeatureValue.recipe.in_(names))
        .group_by(FeatureValue.recipe, FeatureValue.chamber)
    )
    return {(rcp, ch): t for rcp, ch, t in result.tuples().all()}


def _match_chamber(chambers: list[tuple[str, str]] | None, tool: str | None, chamber: str | None) -> bool:
    """按机台 / 腔室过滤：都不填时全部保留；GDE 查不到腔室时只在不过滤时保留。"""
    if not (tool or chamber):
        return True
    return any((not tool or t == tool) and (not chamber or c == chamber) for t, c in chambers or [])


def _schedule_row(r: RecipeSubscription, status: str, chambers, ops: dict, progress: dict) -> dict:
    """组装 list_schedules 的一项。"""
    times = [t for (rcp, _), t in progress.items() if rcp == r.recipe]
    return {
        "recipe": r.recipe, "gde_sub_id": r.gde_sub_id, "wanted": r.wanted,
        "synced": r.wanted == bool(r.gde_sub_id), "last_error": r.last_error, "status": status,
        "operators": sorted(ops.get(r.recipe, [])),
        "chambers": None if chambers is None else [
            {"tool": t, "chamber": c, "progress": progress.get((r.recipe, c))} for t, c in chambers
        ],
        "progress": max(times) if times else None,
    }


async def list_schedules(recipe: str | None = None, tool: str | None = None,
                         chamber: str | None = None) -> list[dict]:
    """需要订阅或仍有 GDE 订阅的 recipe。recipe 为模糊匹配；tool / chamber 按 GDE 反查到的机台 / 腔室过滤。"""
    cond = [or_(RecipeSubscription.wanted, RecipeSubscription.gde_sub_id.is_not(None))]
    if recipe:
        cond.append(RecipeSubscription.recipe.ilike(f"%{recipe}%"))
    async with db.session() as s:
        rows = (await s.execute(
            select(RecipeSubscription).where(*cond).order_by(RecipeSubscription.recipe)
        )).scalars().all()
        names = [r.recipe for r in rows]
        ops = await _recipe_operators(s, names)
        progress = await _live_progress(s, names)
    statuses, chambers = await asyncio.gather(
        asyncio.gather(*(_status(r.gde_sub_id) for r in rows)),
        asyncio.gather(*(_chambers(r.recipe) for r in rows)),
    )
    return [
        _schedule_row(r, st, chs, ops, progress)
        for r, st, chs in zip(rows, statuses, chambers)
        if _match_chamber(chs, tool, chamber)
    ]


async def _display_names(s, fids: set[str]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """特征和算子的显示名称：({feature_id: 名称}, {(feature_id, 算子): 名称})。"""
    features = await s.execute(
        select(FeatureMeta.feature_id, FeatureMeta.name).where(FeatureMeta.feature_id.in_(fids)))
    operators = await s.execute(
        select(OperatorMeta.feature_id, OperatorMeta.operator, OperatorMeta.name)
        .where(OperatorMeta.feature_id.in_(fids)))
    fnames = dict(features.tuples().all())
    onames = {(fid, op): name for fid, op, name in operators.tuples().all()}
    return fnames, onames


def _tree_operator(fid: str, key: tuple[str, int], rcps: list[str], rows: dict, onames: dict) -> dict:
    """调度树中的一个算子节点，recipe 节点去掉 operators 字段。"""
    op, ver = key
    return {
        "operator": op, "version": ver, "ref": op_ref(fid, op, ver), "name": onames.get((fid, op)),
        "recipes": [{k: v for k, v in rows[r].items() if k != "operators"} for r in sorted(rcps)],
    }


async def schedule_tree(feature_id: str | None = None, recipe: str | None = None, tool: str | None = None,
                        chamber: str | None = None) -> list[dict]:
    """特征 → 算子（生效绑定的版本）→ recipe（订阅状态、进度）→ 机台 / 腔室。筛选条件同 list_schedules，另可按特征过滤。"""
    rows = {r["recipe"]: r for r in await list_schedules(recipe, tool, chamber)}
    q = active_items(BindingItem.feature_id, BindingItem.operator, BindingItem.version, BindingItem.recipe) \
        .where(BindingItem.recipe.in_(list(rows)))
    if feature_id:
        q = q.where(BindingItem.feature_id == feature_id)
    async with db.session() as s:
        items = (await s.execute(q)).tuples().all()
        fnames, onames = await _display_names(s, {i[0] for i in items})
    tree: dict[str, dict[tuple[str, int], list[str]]] = {}
    for fid, op, ver, rcp in items:
        tree.setdefault(fid, {}).setdefault((op, ver), []).append(rcp)
    return [
        {
            "feature_id": fid, "name": fnames.get(fid),
            "operators": [_tree_operator(fid, key, rcps, rows, onames) for key, rcps in sorted(tree[fid].items())],
        }
        for fid in sorted(tree)
    ]


async def set_paused(recipe: str, paused: bool) -> None:
    """暂停或恢复 recipe 的 GDE 订阅。"""
    async with db.session() as s:
        row = await s.get(RecipeSubscription, recipe)
    if row is None:
        raise not_found(f"recipe {recipe} 的订阅")
    if not row.gde_sub_id:
        raise invalid_state("该 recipe 没有 GDE 订阅", "cancelled")
    gde = get_gde()
    await _gde(gde.pause_subscription if paused else gde.resume_subscription, row.gde_sub_id)
