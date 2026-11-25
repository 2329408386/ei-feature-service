#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征值查询。retired 由 operator.status 实时得出，不存进值表。"""

from datetime import date

from sqlalchemy import func, select

from src.model.tables import FeatureValue, Operator
from src.service.refs import require_ref
from src.util import db
from src.util.timeutil import day_range


async def query(operator: str, *, tool=None, chamber=None, recipe=None, wafer=None, lot=None,
                start: date | None = None, end: date | None = None, only_null: bool = False,
                page: int = 1, size: int = 100) -> tuple[list[dict], int]:
    """按算子引用 feature_id/operator@version 查询特征值，按执行结束时间升序分页。返回 (items, total)。"""
    key = require_ref(operator)
    filters = {"tool": tool, "chamber": chamber, "recipe": recipe, "wafer": wafer, "lot": lot}
    cond = _conditions(key, filters, (start, end), only_null)
    return await _page(key, cond, page, size)


def _conditions(key: tuple[str, str, int], filters: dict, period: tuple[date | None, date | None],
                only_null: bool) -> list:
    """组装特征值查询的 WHERE 条件；filters 中值为空的列不参与过滤，period 为 (start, end)，含两端日期。"""
    v = FeatureValue
    fid, op, ver = key
    start, end = period
    cond = [v.feature_id == fid, v.operator == op, v.version == ver]
    cond.extend(getattr(v, col) == val for col, val in filters.items() if val)
    if start:
        cond.append(v.run_end_at >= day_range(start, start)[0])
    if end:
        cond.append(v.run_end_at < day_range(end, end)[1])
    if only_null:
        cond.append(v.value.is_(None))
    return cond


async def _page(key: tuple[str, str, int], cond: list, page: int, size: int) -> tuple[list[dict], int]:
    """按条件分页取值，并附上由 operator.status 实时得出的 retired 标记。"""
    v = FeatureValue
    fid, op, ver = key
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(v).where(*cond))
        retired = await s.scalar(select(Operator.status == "retired").where(
            Operator.feature_id == fid, Operator.name == op, Operator.version == ver))
        rows = (await s.execute(
            select(v).where(*cond).order_by(v.run_end_at).offset((page - 1) * size).limit(size)
        )).scalars().all()
    return [{**{c: getattr(r, c) for c in v.__table__.columns.keys()}, "retired": bool(retired)} for r in rows], total
