#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""GDE 元数据转发：/api/gde。绑定页面按 fleet → 腔室 → recipe → 机台 / 腔室 的顺序调用。"""

import asyncio
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from src.api.schemas import ToolChamberOut
from src.util.errors import gde_unavailable, invalid, not_found
from src.util.gde import GdeError, GdeNotFound, get_gde
from src.util.timeutil import day_range, today

router = APIRouter(prefix="/gde", tags=["GDE 元数据"])
DEFAULT_DAYS = 30  # fleet 选腔室默认看最近 1 个月


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except GdeNotFound as e:
        raise not_found(str(e).removesuffix(" 不存在")) from None
    except GdeError as e:
        raise gde_unavailable(str(e)) from None


def _pairs(rows) -> list[dict]:
    return [{"tool": t, "chamber": c} for t, c in rows]


@router.get("/fleets", response_model=list[str])
async def fleets():
    """全部 fleet。"""
    return await _call(get_gde().list_fleets)


@router.get("/fleets/{fleet}/chambers", response_model=list[ToolChamberOut])
async def fleet_chambers(fleet: str, start: date | None = None, end: date | None = None):
    """fleet 下在 [start, end]（默认最近 1 个月）内有执行的机台、腔室。"""
    end = end or today()
    start = start or end - timedelta(days=DEFAULT_DAYS)
    if start > end:
        raise invalid("VALIDATION_ERROR", "参数错误", [{"path": "start", "message": "开始日期不能晚于结束日期"}])
    lo, hi = day_range(start, end)
    return _pairs(await _call(get_gde().fleet_chambers, fleet, lo, hi))


@router.get("/recipes", response_model=list[str])
async def recipes(q: str | None = None, limit: Annotated[int, Query(ge=1, le=1000)] = 50):
    """全部 recipe；q 不区分大小写的模糊匹配，供下拉框输入关键字后过滤。"""
    rows = await _call(get_gde().list_recipes)
    if q:
        rows = [r for r in rows if q.lower() in r.lower()]
    return rows[:limit]


@router.get("/recipes/{recipe}/chambers", response_model=list[ToolChamberOut])
async def recipe_chambers(recipe: str):
    """跑过该 recipe 的全部机台、腔室。"""
    return _pairs(await _call(get_gde().recipe_chambers, recipe))
