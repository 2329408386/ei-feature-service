#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征值查询。retired 由 feature.status 实时得出，不存进值表。"""

from datetime import date

from sqlalchemy import func, select

from src.model.tables import Feature, FeatureValue
from src.util import db
from src.util.errors import invalid
from src.util.timeutil import day_range


async def query(feature: str, *, tool=None, chamber=None, recipe=None, wafer=None, lot=None,
                start: date | None = None, end: date | None = None, only_null: bool = False,
                page: int = 1, size: int = 100) -> tuple[list[dict], int]:
    """按 id@version 查询特征值，按执行结束时间升序分页。返回 (items, total)。"""
    fid, _, ver = feature.partition("@")
    if not ver:
        raise invalid("VALIDATION_ERROR", "feature 必须形如 id@version")
    filters = {"tool": tool, "chamber": chamber, "recipe": recipe, "wafer": wafer, "lot": lot}
    cond = _conditions(fid, ver, filters, start, end, only_null)
    return await _page(fid, ver, cond, page, size)


def _conditions(fid: str, ver: str, filters: dict, start: date | None, end: date | None, only_null: bool) -> list:
    """组装特征值查询的 WHERE 条件；filters 中值为空的列不参与过滤。"""
    v = FeatureValue
    cond = [v.feature_id == fid, v.version == ver]
    cond.extend(getattr(v, col) == val for col, val in filters.items() if val)
    if start:
        cond.append(v.run_end_at >= day_range(start, start)[0])
    if end:
        cond.append(v.run_end_at < day_range(end, end)[1])
    if only_null:
        cond.append(v.value.is_(None))
    return cond


async def _page(fid: str, ver: str, cond: list, page: int, size: int) -> tuple[list[dict], int]:
    """按条件分页取值，并附上由 feature.status 实时得出的 retired 标记。"""
    v = FeatureValue
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(v).where(*cond))
        is_retired = select(Feature.status == "retired").where(Feature.feature_id == fid, Feature.version == ver)
        retired = await s.scalar(is_retired)
        rows = (await s.execute(
            select(v).where(*cond).order_by(v.run_end_at).offset((page - 1) * size).limit(size)
        )).scalars().all()
    return [{**{c: getattr(r, c) for c in v.__table__.columns.keys()}, "retired": bool(retired)} for r in rows], total
