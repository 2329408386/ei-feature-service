#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子接口：/api/operators"""

from typing import Annotated

from fastapi import APIRouter, Query

from src.api.schemas import (
    OperatorDetail,
    OperatorIn,
    OperatorOut,
    OperatorRetireOut,
    Page,
    RetireIn,
    ReviewIn,
    Status,
    TrialIn,
    TrialOut,
)
from src.service import operator as svc
from src.util.auth import User

router = APIRouter(prefix="/operators", tags=["算子"])
PATH = "/{feature_id}/{name}/{version}"


@router.post("", status_code=201, response_model=OperatorOut)
async def create(body: OperatorIn, user: User):
    """新增算子；同名算子再次提交时生成新版本（版本号 +1），不改变特征版本。"""
    return await svc.create(body, user)


@router.get("", response_model=Page[OperatorOut])
async def list_operators(
    feature_id: str | None = None,
    name: str | None = None,
    status: Status | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """分页查询算子，可按特征、算子名、状态、关键字过滤。"""
    rows, total = await svc.list_operators(feature_id, name, status, q, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get(PATH, response_model=OperatorDetail)
async def get_operator(feature_id: str, name: str, version: int):
    """算子详情，附带代码。"""
    row, code = await svc.get_operator(feature_id, name, version)
    return {**OperatorOut.model_validate(row).model_dump(), "code": code}


@router.delete(PATH, status_code=204)
async def delete_draft(feature_id: str, name: str, version: int, user: User):
    """删除草稿（只有提交人可以删除）。"""
    await svc.delete_draft(feature_id, name, version, user)


@router.post(f"{PATH}/submit", response_model=OperatorOut)
async def submit(feature_id: str, name: str, version: int, user: User):
    """草稿提交评审。"""
    return await svc.submit(feature_id, name, version, user)


@router.post(f"{PATH}/review", response_model=OperatorOut)
async def review(feature_id: str, name: str, version: int, body: ReviewIn, user: User):
    """评审：通过（需试跑过、特征已发布）则发布，驳回则退回草稿。"""
    return await svc.review(feature_id, name, version, body, user)


@router.post(f"{PATH}/retire", response_model=OperatorRetireOut)
async def retire(feature_id: str, name: str, version: int, body: RetireIn, user: User):
    """下线算子，并自动修改引用它的算子绑定。"""
    row, affected = await svc.retire(feature_id, name, version, body.reason, user)
    return {"operator": row, "affected": affected}


@router.post(f"{PATH}/trial", response_model=TrialOut)
async def trial(feature_id: str, name: str, version: int, body: TrialIn):
    """试跑：执行一次 fetch + compute，结果不写库。"""
    return await svc.trial(feature_id, name, version, body.run_id)
