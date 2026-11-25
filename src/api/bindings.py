#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子绑定接口：/api/bindings"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Header
from fastapi.responses import Response

from src.api.schemas import (
    BindingSubmitIn,
    ImportDoc,
    ImportOut,
    OperatorBindingOut,
    OperatorRevisionsOut,
    RevisionOut,
    RevisionReviewIn,
)
from src.service import binding as svc
from src.util.auth import User

router = APIRouter(prefix="/bindings", tags=["算子绑定"])
OP = "/{feature_id}/{operator}"
REV = OP + "/revisions/{revision}"
StatusFilter = Literal["active", "pending"]


def _filters(feature_id=None, operator=None, tool=None, chamber=None, recipe=None, status=None) -> dict:
    return {"feature_id": feature_id, "operator": operator, "tool": tool, "chamber": chamber, "recipe": recipe,
            "status": status}


@router.get("/export")
async def export(feature_id: str | None = None, operator: str | None = None, tool: str | None = None,
                 chamber: str | None = None, recipe: str | None = None, status: StatusFilter | None = None):
    """导出生效修订的绑定 JSON（下载文件）。"""
    doc = await svc.export(**_filters(feature_id, operator, tool, chamber, recipe, status))
    return Response(
        json.dumps(doc, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ei-feature-service-bindings.json"},
    )


@router.post("/import", response_model=ImportOut)
async def import_bindings(body: ImportDoc, user: User, dry_run: bool = True):
    """导入：dry_run=true 只校验；false 时为每个 new 的算子生成待审核修订。"""
    return {"items": await svc.import_bindings(body.bindings, dry_run, user)}


@router.get("", response_model=dict[str, list[OperatorBindingOut]])
async def list_bindings(feature_id: str | None = None, operator: str | None = None, tool: str | None = None,
                        chamber: str | None = None, recipe: str | None = None, status: StatusFilter | None = None):
    """按条件查询各算子的生效修订和待审核修订。"""
    return {"items": await svc.list_bindings(**_filters(feature_id, operator, tool, chamber, recipe, status))}


@router.get(OP, response_model=OperatorRevisionsOut | RevisionOut)
async def get_operator(feature_id: str, operator: str, revision: int | None = None):
    """不带 revision：全部修订（新的在前）；带 revision：单个修订。"""
    result = await svc.get_revisions(feature_id, operator, revision)
    if revision is not None:
        return RevisionOut.model_validate(result)
    return {"feature_id": feature_id, "operator": operator, "revisions": result}


@router.post(OP, status_code=201, response_model=RevisionOut)
async def submit(feature_id: str, operator: str, body: BindingSubmitIn, user: User,
                 x_client: Annotated[str | None, Header()] = None):
    """提交新修订（待审核）；base_revision 用于检测并发修改。"""
    source = "console" if x_client == "console" else "api"
    return await svc.submit(feature_id, operator, body, body.base_revision, user, source)


@router.post(f"{REV}/review", response_model=RevisionOut)
async def review(feature_id: str, operator: str, revision: int, body: RevisionReviewIn, user: User):
    """审核待审核修订：通过后生效，驳回则结束。"""
    return await svc.review(feature_id, operator, revision, body.decision, body.comment, user)


@router.post(f"{REV}/withdraw", response_model=RevisionOut)
async def withdraw(feature_id: str, operator: str, revision: int, user: User):
    """提交人撤回自己的待审核修订。"""
    return await svc.withdraw(feature_id, operator, revision, user)
