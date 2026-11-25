#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征值接口：/api/values"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from src.api.schemas import Page, ValueOut
from src.service import value as svc

router = APIRouter(prefix="/values", tags=["特征值"])


@router.get("", response_model=Page[ValueOut])
async def query(
    feature: Annotated[str, Query(description="id@version")],
    tool: str | None = None,
    chamber: str | None = None,
    recipe: str | None = None,
    wafer: str | None = None,
    lot: str | None = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    only_null: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    """按执行结束时间升序。"""
    items, total = await svc.query(
        feature, tool=tool, chamber=chamber, recipe=recipe, wafer=wafer, lot=lot,
        start=from_, end=to, only_null=only_null, page=page, size=page_size,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}
