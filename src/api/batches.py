#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""计算批次接口：/api/batches"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from src.api.schemas import BatchIn, BatchOut, Page
from src.service import batch as svc
from src.util.auth import User

router = APIRouter(prefix="/batches", tags=["批次"])


@router.post("", status_code=201, response_model=BatchOut)
async def create(body: BatchIn, user: User):
    """提交补算（fill）或重算（overwrite），进入队列由后台执行。"""
    return await svc.create(body, user)


@router.get("", response_model=Page[BatchOut])
async def list_batches(
    chamber: str | None = None,
    mode: Literal["live", "fill", "overwrite"] | None = None,
    status: Literal["queued", "running", "done", "failed", "cancelled"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """分页查询批次，新的在前。"""
    rows, total = await svc.list_batches(chamber, mode, status, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/{batch_id}", response_model=BatchOut)
async def get(batch_id: int):
    """查询单个批次。"""
    return await svc.get(batch_id)


@router.post("/{batch_id}/cancel", response_model=BatchOut)
async def cancel(batch_id: int, _: User):
    """取消排队中或运行中的补算 / 重算批次。"""
    return await svc.cancel(batch_id)
