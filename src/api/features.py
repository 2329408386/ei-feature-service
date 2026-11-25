#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征接口：/api/features。特征只有元数据，计算代码在算子里（/api/operators）。"""

from typing import Annotated

from fastapi import APIRouter, Body, Query

from src.api.schemas import FeatureDetail, FeatureOut, FeatureRetireOut, OperatorOut, Page, RetireIn, ReviewIn, Status
from src.service import feature as svc
from src.util.auth import User

router = APIRouter(prefix="/features", tags=["特征"])
PATH = "/{feature_id}/{version}"


@router.post("", status_code=201, response_model=FeatureOut)
async def create(user: User, spec: Annotated[dict, Body(description="feature.json 内容")]):
    """新增特征（或特征的新版本），校验通过后生成草稿。"""
    return await svc.create(spec, user)


@router.get("", response_model=Page[FeatureOut])
async def list_features(
    status: Status | None = None,
    q: str | None = None,
    feature_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """分页查询特征，可按状态、关键字、特征 ID 过滤。"""
    rows, total = await svc.list_features(status, q, feature_id, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get(PATH, response_model=FeatureDetail)
async def get_feature(feature_id: str, version: str):
    """特征详情，附带该特征下的全部算子。"""
    row, ops = await svc.get_feature(feature_id, version)
    return {**FeatureOut.model_validate(row).model_dump(), "operators": [OperatorOut.model_validate(o) for o in ops]}


@router.delete(PATH, status_code=204)
async def delete_draft(feature_id: str, version: str, user: User):
    """删除草稿（只有提交人可以删除）。"""
    await svc.delete_draft(feature_id, version, user)


@router.post(f"{PATH}/submit", response_model=FeatureOut)
async def submit(feature_id: str, version: str, user: User):
    """草稿提交评审。"""
    return await svc.submit(feature_id, version, user)


@router.post(f"{PATH}/review", response_model=FeatureOut)
async def review(feature_id: str, version: str, body: ReviewIn, user: User):
    """评审：通过则发布（旧的已发布版本自动下线），驳回则退回草稿。"""
    return await svc.review(feature_id, version, body, user)


@router.post(f"{PATH}/retire", response_model=FeatureRetireOut)
async def retire(feature_id: str, version: str, body: RetireIn, user: User):
    """下线整个特征：已发布的算子一起下线，并从绑定中移除。"""
    row, refs, affected = await svc.retire(feature_id, version, body.reason, user)
    return {"feature": row, "operators": refs, "affected": affected}
