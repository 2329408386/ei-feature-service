#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征 / 算子的可修改信息：显示名称、机台类型（仅特征）。

ID（feature_id、算子名）永远不变，引用、绑定、特征值都按 ID；显示名称只用于页面展示和搜索，
按 feature_id / (feature_id, 算子名) 保存，与版本无关，修改不需要评审。
"""

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from src.model.tables import Feature, FeatureMeta, Operator, OperatorMeta
from src.util import db
from src.util.errors import not_found
from src.util.timeutil import now


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


async def attach_features(s, rows: list[Feature]) -> list[Feature]:
    """把 feature_meta 的名称、机台类型填到 Feature 对象上（display_name / tool_type）。"""
    ids = {r.feature_id for r in rows}
    if ids:
        metas = {m.feature_id: m for m in (await s.execute(
            select(FeatureMeta).where(FeatureMeta.feature_id.in_(ids))
        )).scalars().all()}
        for r in rows:
            m = metas.get(r.feature_id)
            r.display_name, r.tool_type = (m.name, m.tool_type) if m else (None, None)
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


async def set_feature(feature_id: str, fields: dict, user: str) -> dict:
    """修改特征的名称 / 机台类型（fields 中只含要改的键），返回修改后的 {feature_id, name, tool_type}。"""
    async with db.session() as s:
        if await s.scalar(select(Feature.feature_id).where(Feature.feature_id == feature_id).limit(1)) is None:
            raise not_found(f"特征 {feature_id}")
        values = {k: _clean(v) for k, v in fields.items() if k in ("name", "tool_type")}
        row = (await s.execute(
            insert(FeatureMeta).values(feature_id=feature_id, updated_by=user, **values)
            .on_conflict_do_update(index_elements=["feature_id"], set_={**values, "updated_by": user, "updated_at": now()})
            .returning(FeatureMeta)
        )).scalar_one()
        return {"feature_id": feature_id, "name": row.name, "tool_type": row.tool_type}


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


async def tool_types() -> list[str]:
    """已用过的机台类型，供筛选下拉框。"""
    async with db.session() as s:
        return list((await s.execute(
            select(FeatureMeta.tool_type).where(FeatureMeta.tool_type.is_not(None)).distinct()
            .order_by(FeatureMeta.tool_type)
        )).scalars().all())
