#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征接口：/api/features。特征只有元数据，计算代码在算子里（/api/operators）。"""

from typing import Annotated

from fastapi import APIRouter, Body, Query, Response

from src.api.schemas import (
    DeleteImpact,
    FeatureDetail,
    FeatureRestoreOut,
    FeatureMetaIn,
    FeatureMetaOut,
    FeatureOut,
    FeatureRetireOut,
    OperatorOut,
    Page,
    RestoreIn,
    RetireIn,
    ReviewIn,
    Status,
)
from src.service import feature as svc
from src.service import meta
from src.util.auth import User

router = APIRouter(prefix="/features", tags=["特征"])
PATH = "/{feature_id}/{version}"


@router.post("", status_code=201, response_model=FeatureOut)
async def create(user: User, spec: Annotated[dict, Body(description="feature.json 内容")]):
    """新增特征（不填 id，后台生成，name 必填），或带已有 id 提交新版本；校验通过后生成草稿。"""
    return await svc.create(spec, user)


@router.get("", response_model=Page[FeatureOut])
async def list_features(
    status: Status | None = None,
    q: str | None = None,
    feature_id: str | None = None,
    tool: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    """分页查询特征，可按状态、关键字（ID / 名称 / 描述）、特征 ID、关联机台过滤。"""
    rows, total = await svc.list_features(status, q, feature_id, tool, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/tools", response_model=list[str])
async def tools():
    """所有特征关联的机台（生效绑定的 recipe 经 GDE 反查），供筛选下拉框。"""
    return await meta.all_tools()


@router.patch("/{feature_id}", response_model=FeatureMetaOut)
async def update_meta(feature_id: str, body: FeatureMetaIn, user: User):
    """修改特征的显示名称。feature_id 不变，对全部版本生效，不需要评审。"""
    return await meta.set_feature(feature_id, body.name, user)


@router.get(PATH, response_model=FeatureDetail)
async def get_feature(feature_id: str, version: str):
    """特征详情，附带该特征下的全部算子。"""
    row, ops = await svc.get_feature(feature_id, version)
    return {**FeatureOut.model_validate(row).model_dump(), "operators": [OperatorOut.model_validate(o) for o in ops]}


@router.delete(PATH, response_model=DeleteImpact, responses={204: {"description": "已删除"}})
async def delete_feature(feature_id: str, version: str, user: User, dry_run: bool = False):
    """删除特征版本：自己的草稿或已下线的。最后一个版本时连同全部算子、特征值、绑定修订一起删除，不可恢复。
    dry_run=true 只返回影响范围（200），用于弹窗警告。"""
    out = await svc.remove(feature_id, version, user, dry_run)
    return out if dry_run else Response(status_code=204)


@router.post(f"{PATH}/submit", response_model=FeatureOut)
async def submit(feature_id: str, version: str, user: User):
    """草稿提交评审。"""
    return await svc.submit(feature_id, version, user)


@router.post(f"{PATH}/review", response_model=FeatureOut)
async def review(feature_id: str, version: str, body: ReviewIn, user: User):
    """评审：通过则发布（旧的已发布版本自动下线），驳回则退回草稿。"""
    return await svc.review(feature_id, version, body, user)


@router.post(f"{PATH}/restore", response_model=FeatureRestoreOut)
async def restore(feature_id: str, version: str, body: RestoreIn, user: User):
    """已下线的特征版本重新上线（不再评审）；可把随它下线的算子一并上线。绑定需重新提交。"""
    row, refs = await svc.restore(feature_id, version, body.comment, body.operators, user)
    return {"feature": row, "operators": refs}


@router.post(f"{PATH}/retire", response_model=FeatureRetireOut)
async def retire(feature_id: str, version: str, body: RetireIn, user: User):
    """下线整个特征：已发布的算子一起下线，并从绑定中移除。"""
    row, refs, affected = await svc.retire(feature_id, version, body.reason, user)
    return {"feature": row, "operators": refs, "affected": affected}
