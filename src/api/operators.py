#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子接口：/api/operators"""

from typing import Annotated

from fastapi import APIRouter, Query, Response

from src.api.schemas import (
    DeleteImpact,
    OperatorDetail,
    OperatorIn,
    OperatorMetaIn,
    OperatorMetaOut,
    OperatorOut,
    OperatorQuery,
    OperatorRetireOut,
    Page,
    RestoreIn,
    RetireIn,
    ReviewIn,
    TrialIn,
    TrialOut,
)
from src.service import meta
from src.service import operator as svc
from src.util.auth import User

router = APIRouter(prefix="/operators", tags=["算子"])
PATH = "/{feature_id}/{name}/{version}"


@router.post("", status_code=201, response_model=OperatorOut)
async def create(body: OperatorIn, user: User):
    """新增算子（不填 id，后台生成，name 必填）；operator.json 带已有算子的 id 时生成新版本（版本号 +1）。"""
    return await svc.create(body, user)


@router.get("", response_model=Page[OperatorOut])
async def list_operators(query: Annotated[OperatorQuery, Query()]):
    """分页查询算子，可按特征、算子名、状态、关键字过滤。"""
    rows, total = await svc.list_operators(query)
    return {"items": rows, "total": total, "page": query.page, "page_size": query.page_size}


@router.patch("/{feature_id}/{name}", response_model=OperatorMetaOut)
async def update_meta(feature_id: str, name: str, body: OperatorMetaIn, user: User):
    """修改算子的显示名称。算子名（引用中的 operator）不变，对全部版本生效，不需要评审。"""
    return await meta.set_operator(feature_id, name, body.name, user)


@router.get(PATH, response_model=OperatorDetail)
async def get_operator(feature_id: str, name: str, version: int):
    """算子详情，附带代码。"""
    row, code = await svc.get_operator(feature_id, name, version)
    return {**OperatorOut.model_validate(row).model_dump(), "code": code}


@router.delete(PATH, response_model=DeleteImpact, responses={204: {"description": "已删除"}})
async def delete_operator(feature_id: str, name: str, version: int, user: User, dry_run: bool = False):
    """删除算子版本：自己的草稿或已下线的；连同特征值、文件删除，最后一个版本时连同绑定修订和名称，不可恢复。

    dry_run=true 只返回影响范围（200），用于弹窗警告。"""
    out = await svc.remove(feature_id, name, version, user, dry_run)
    return out if dry_run else Response(status_code=204)


@router.post(f"{PATH}/submit", response_model=OperatorOut)
async def submit(feature_id: str, name: str, version: int, user: User):
    """草稿提交评审。"""
    return await svc.submit(feature_id, name, version, user)


@router.post(f"{PATH}/review", response_model=OperatorOut)
async def review(feature_id: str, name: str, version: int, body: ReviewIn, user: User):
    """评审：通过（需试跑过、特征已发布）则发布，驳回则退回草稿。"""
    return await svc.review(feature_id, name, version, body, user)


@router.post(f"{PATH}/restore", response_model=OperatorOut)
async def restore(feature_id: str, name: str, version: int, body: RestoreIn, user: User):
    """已下线的算子重新上线（不再评审）；特征须已发布。绑定需重新提交。"""
    return await svc.restore(feature_id, name, version, body.comment, user)


@router.post(f"{PATH}/retire", response_model=OperatorRetireOut)
async def retire(feature_id: str, name: str, version: int, body: RetireIn, user: User):
    """下线算子，并自动修改引用它的算子绑定。"""
    row, affected = await svc.retire(feature_id, name, version, body.reason, user)
    return {"operator": row, "affected": affected}


@router.post(f"{PATH}/trial", response_model=TrialOut)
async def trial(feature_id: str, name: str, version: int, body: TrialIn):
    """试跑：执行一次 fetch + compute，结果不写库。"""
    return await svc.trial(feature_id, name, version, body.run_id)
