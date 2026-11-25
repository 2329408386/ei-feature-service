#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""调度接口：/api/schedules"""

from fastapi import APIRouter

from src.api.schemas import ScheduleOut
from src.service import schedule as svc
from src.util.auth import User

router = APIRouter(prefix="/schedules", tags=["调度"])


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(tool: str | None = None, chamber: str | None = None):
    """各腔室的订阅状态与处理进度。"""
    return await svc.list_schedules(tool, chamber)


@router.post("/{chamber}/pause", status_code=204)
async def pause(chamber: str, _: User):
    """暂停腔室的 GDE 订阅。"""
    await svc.set_paused(chamber, True)


@router.post("/{chamber}/resume", status_code=204)
async def resume(chamber: str, _: User):
    """恢复腔室的 GDE 订阅。"""
    await svc.set_paused(chamber, False)
