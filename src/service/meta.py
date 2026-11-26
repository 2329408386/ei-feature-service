#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征 / 算子的可修改信息（显示名称），以及特征关联的机台。

ID（feature_id、算子名）永远不变，引用、绑定、特征值都按 ID；显示名称只用于页面展示和搜索，
按 feature_id / (feature_id, 算子名) 保存，与版本无关，修改不需要评审。

特征关联的机台不手填：该特征下各算子生效绑定的 recipe，经 GDE recipe_chambers 反查到的机台（去重排序）。
反查结果按 recipe 缓存 RECIPE_CHAMBERS_TTL_S 秒；GDE 不可用时该 recipe 不计入。
"""

import asyncio
import time

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from src.model.tables import BindingItem, Feature, FeatureMeta, Operator, OperatorMeta
from src.service.subscription import active_items
from src.util import db
from src.util.config import RECIPE_CHAMBERS_TTL_S
from src.util.errors import not_found
from src.util.gde import GdeError, get_gde
from src.util.timeutil import now

_cache: dict[str, tuple[float, list[tuple[str, str]] | None]] = {}


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


async def recipe_chambers(recipe: str) -> list[tuple[str, str]] | None:
    """GDE 反查 recipe 的 [(tool, chamber)]，带短时缓存；GDE 不可用或 recipe 不存在返回 None。"""
    hit = _cache.get(recipe)
    if hit and time.monotonic() - hit[0] < RECIPE_CHAMBERS_TTL_S:
        return hit[1]
    try:
        val = await asyncio.to_thread(get_gde().recipe_chambers, recipe)
    except GdeError:
        val = None
    _cache[recipe] = (time.monotonic(), val)
    return val


async def feature_tools(s, feature_ids: set[str] | None = None) -> dict[str, list[str]]:
    """{feature_id: [机台]}：只含有生效绑定的特征。feature_ids 为 None 表示全部。"""
    q = active_items(BindingItem.feature_id, BindingItem.recipe).distinct()
    if feature_ids is not None:
        q = q.where(BindingItem.feature_id.in_(feature_ids))
    pairs = (await s.execute(q)).tuples().all()
    recipes = sorted({r for _, r in pairs})
    chambers = dict(zip(recipes, await asyncio.gather(*(recipe_chambers(r) for r in recipes))))
    out: dict[str, set[str]] = {}
    for fid, r in pairs:
        out.setdefault(fid, set()).update(t for t, _ in chambers[r] or [])
    return {fid: sorted(ts) for fid, ts in out.items()}


async def all_tools() -> list[str]:
    """所有特征关联的机台（去重排序），供特征页筛选下拉框。"""
    async with db.session() as s:
        return sorted({t for ts in (await feature_tools(s)).values() for t in ts})


async def attach_features(s, rows: list[Feature], tools: dict[str, list[str]] | None = None) -> list[Feature]:
    """把显示名称（feature_meta）和关联机台填到 Feature 对象上（display_name / tools）。"""
    ids = {r.feature_id for r in rows}
    if ids:
        names = {m.feature_id: m.name for m in (await s.execute(
            select(FeatureMeta).where(FeatureMeta.feature_id.in_(ids))
        )).scalars().all()}
        if tools is None:
            tools = await feature_tools(s, ids)
        for r in rows:
            r.display_name = names.get(r.feature_id)
            r.tools = tools.get(r.feature_id, [])
    return rows


async def attach_operators(s, rows: list[Operator]) -> list[Operator]:
    """把 operator_meta 的名称填到 Operator 对象上（display_name）。"""
    keys = {(r.feature_id, r.name) for r in rows}
    if keys:
        names = {(m.feature_id, m.operator): m.name for m in (await s.execute(
            select(OperatorMeta).where(tuple_(OperatorMeta.feature_id, OperatorMeta.operator).in_(keys))
        )).scalars().all()}
        for r in rows:
            r.display_name = names.get((r.feature_id, r.name))
    return rows


async def set_feature(feature_id: str, name: str | None, user: str) -> dict:
    """修改特征的显示名称，返回 {feature_id, name}。"""
    async with db.session() as s:
        if await s.scalar(select(Feature.feature_id).where(Feature.feature_id == feature_id).limit(1)) is None:
            raise not_found(f"特征 {feature_id}")
        name = _clean(name)
        await s.execute(
            insert(FeatureMeta).values(feature_id=feature_id, name=name, updated_by=user)
            .on_conflict_do_update(index_elements=["feature_id"], set_={"name": name, "updated_by": user, "updated_at": now()})
        )
    return {"feature_id": feature_id, "name": name}


async def set_operator(feature_id: str, operator: str, name: str | None, user: str) -> dict:
    """修改算子的显示名称，返回 {feature_id, operator, name}。"""
    async with db.session() as s:
        exists = await s.scalar(select(Operator.name).where(
            Operator.feature_id == feature_id, Operator.name == operator).limit(1))
        if exists is None:
            raise not_found(f"算子 {feature_id}/{operator}")
        name = _clean(name)
        await s.execute(
            insert(OperatorMeta).values(feature_id=feature_id, operator=operator, name=name, updated_by=user)
            .on_conflict_do_update(index_elements=["feature_id", "operator"],
                                   set_={"name": name, "updated_by": user, "updated_at": now()})
        )
    return {"feature_id": feature_id, "operator": operator, "name": name}
