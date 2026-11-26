#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""调度接口：/api/schedules，以 recipe 为键。"""

from fastapi import APIRouter

from src.api.schemas import ScheduleOut
from src.service import schedule as svc
from src.util.auth import User

router = APIRouter(prefix="/schedules", tags=["调度"])


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(recipe: str | None = None, tool: str | None = None, chamber: str | None = None):
    """各 recipe 的订阅状态、算子、机台 / 腔室与处理进度。"""
    return await svc.list_schedules(recipe, tool, chamber)


@router.post("/{recipe}/pause", status_code=204)
async def pause(recipe: str, _: User):
    """暂停 recipe 的 GDE 订阅。"""
    await svc.set_paused(recipe, True)


@router.post("/{recipe}/resume", status_code=204)
async def resume(recipe: str, _: User):
    """恢复 recipe 的 GDE 订阅。"""
    await svc.set_paused(recipe, False)
