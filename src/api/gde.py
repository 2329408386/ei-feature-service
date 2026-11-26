#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""GDE 元数据转发：/api/gde。

- 绑定页面：recipes?q=（模糊搜索）、recipes/{recipe}/chambers（只读展示该 recipe 当前覆盖的机台 / 腔室）；
- 试跑选 run_id：时间范围 + fleet → fleets/{fleet}/chambers → chambers/{chamber}/recipes → runs。
"""

import asyncio
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from src.api.schemas import RunOut, ToolChamberOut
from src.util.errors import gde_unavailable, invalid, not_found
from src.util.gde import GdeError, GdeNotFound, get_gde
from src.util.timeutil import day_range, today

router = APIRouter(prefix="/gde", tags=["GDE 元数据"])
DEFAULT_DAYS = 30  # fleet 选腔室默认看最近 1 个月
RUNS_MAX = 500  # 执行清单最多返回条数（新的在前）


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


def _period(start: date | None, end: date | None) -> tuple:
    """[start, end]（默认最近 1 个月）转成时间范围；开始晚于结束时报参数错误。"""
    end = end or today()
    start = start or end - timedelta(days=DEFAULT_DAYS)
    if start > end:
        raise invalid("VALIDATION_ERROR", "参数错误", [{"path": "start", "message": "开始日期不能晚于结束日期"}])
    return day_range(start, end)


@router.get("/fleets/{fleet}/chambers", response_model=list[ToolChamberOut])
async def fleet_chambers(fleet: str, start: date | None = None, end: date | None = None):
    """fleet 下在 [start, end]（默认最近 1 个月）内有执行的机台、腔室。"""
    return _pairs(await _call(get_gde().fleet_chambers, fleet, *_period(start, end)))


@router.get("/chambers/{chamber}/recipes", response_model=list[str])
async def chamber_recipes(chamber: str, start: date | None = None, end: date | None = None):
    """腔室在 [start, end]（默认最近 1 个月）内跑过的 recipe。"""
    return await _call(get_gde().chamber_recipes, chamber, *_period(start, end))


@router.get("/runs", response_model=list[RunOut])
async def runs(recipe: str, chamber: str | None = None, start: date | None = None, end: date | None = None,
               limit: Annotated[int, Query(ge=1, le=RUNS_MAX)] = 200):
    """recipe 在 [start, end]（默认最近 1 个月）内的执行，可限定腔室；按结束时间倒序，最多 limit 条。供试跑选 run_id。"""
    rows = await _call(get_gde().list_runs, recipe, *_period(start, end), chamber)
    return [
        {k: getattr(r, k) for k in ("run_id", "tool", "chamber", "recipe", "lot", "wafer", "start_at", "end_at")}
        for r in sorted(rows, key=lambda r: r.end_at, reverse=True)[:limit]
    ]


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
