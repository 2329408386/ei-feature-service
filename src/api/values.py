#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征值接口：/api/values"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.schemas import Page, ValueOut
from src.service import value as svc

router = APIRouter(prefix="/values", tags=["特征值"])


class ValueQuery(BaseModel):
    """特征值查询参数。"""

    model_config = ConfigDict(populate_by_name=True)

    operator: str = Field(description="feature_id/operator@version")
    tool: str | None = None
    chamber: str | None = None
    recipe: str | None = None
    wafer: str | None = None
    lot: str | None = None
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    only_null: bool = False
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)


@router.get("", response_model=Page[ValueOut])
async def query(q: Annotated[ValueQuery, Query()]):
    """按执行结束时间升序。"""
    items, total = await svc.query(
        q.operator, tool=q.tool, chamber=q.chamber, recipe=q.recipe, wafer=q.wafer, lot=q.lot,
        start=q.from_, end=q.to, only_null=q.only_null, page=q.page, size=q.page_size,
    )
    return {"items": items, "total": total, "page": q.page, "page_size": q.page_size}
