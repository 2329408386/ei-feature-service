#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""腔室绑定接口：/api/chamber-bindings"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Header
from fastapi.responses import Response

from src.api.schemas import (
    BindingSubmitIn,
    ChamberBindingOut,
    ChamberRevisionsOut,
    ImportDoc,
    ImportOut,
    RevisionOut,
    RevisionReviewIn,
)
from src.service import binding as svc
from src.util.auth import User

router = APIRouter(prefix="/chamber-bindings", tags=["腔室绑定"])
REV = "/{chamber}/revisions/{revision}"


def _filters(tool: str | None = None, chamber: str | None = None, recipe: str | None = None,
             feature: str | None = None, status: Literal["active", "pending"] | None = None) -> dict:
    return {"tool": tool, "chamber": chamber, "recipe": recipe, "feature": feature, "status": status}


# 固定路径要写在 /{chamber} 之前，否则 export 会被当成腔室名
@router.get("/export")
async def export(tool: str | None = None, chamber: str | None = None, recipe: str | None = None,
                 feature: str | None = None, status: Literal["active", "pending"] | None = None):
    """导出生效修订的绑定 JSON（下载文件）。"""
    doc = await svc.export(**_filters(tool, chamber, recipe, feature, status))
    return Response(
        json.dumps(doc, ensure_ascii=False, indent=2), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ei-feature-service-bindings.json"},
    )


@router.post("/import", response_model=ImportOut)
async def import_bindings(body: ImportDoc, user: User, dry_run: bool = True):
    """导入：dry_run=true 只校验；false 时为每个 new 的腔室生成待审核修订。"""
    return {"items": await svc.import_bindings(body.bindings, dry_run, user)}


@router.get("", response_model=dict[str, list[ChamberBindingOut]])
async def list_bindings(tool: str | None = None, chamber: str | None = None, recipe: str | None = None,
                        feature: str | None = None, status: Literal["active", "pending"] | None = None):
    """按条件查询各腔室的生效修订和待审核修订。"""
    return {"items": await svc.list_bindings(**_filters(tool, chamber, recipe, feature, status))}


@router.get("/{chamber}", response_model=ChamberRevisionsOut | RevisionOut)
async def get_chamber(chamber: str, revision: int | None = None):
    """不带 revision：全部修订（新的在前）；带 revision：单个修订。"""
    result = await svc.get_revisions(chamber, revision)
    if revision is not None:
        return RevisionOut.model_validate(result)
    return {"chamber": chamber, "revisions": result}


@router.post("/{chamber}", status_code=201, response_model=RevisionOut)
async def submit(chamber: str, body: BindingSubmitIn, user: User,
                 x_client: Annotated[str | None, Header()] = None):
    """提交新修订（待审核）；base_revision 用于检测并发修改。"""
    source = "console" if x_client == "console" else "api"
    return await svc.submit(chamber, body, body.base_revision, user, source)


@router.post(f"{REV}/review", response_model=RevisionOut)
async def review(chamber: str, revision: int, body: RevisionReviewIn, user: User):
    """审核待审核修订：通过后生效，驳回则结束。"""
    return await svc.review(chamber, revision, body.decision, body.comment, user)


@router.post(f"{REV}/withdraw", response_model=RevisionOut)
async def withdraw(chamber: str, revision: int, user: User):
    """提交人撤回自己的待审核修订。"""
    return await svc.withdraw(chamber, revision, user)
