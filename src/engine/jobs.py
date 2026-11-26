#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""定时任务：实时批次收尾、把未同步的 recipe 订阅同步到 GDE。"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import update

from src.model.tables import FeatureBatch
from src.service import subscription
from src.util import db
from src.util.timeutil import now

log = logging.getLogger(__name__)
CLOSE_EVERY_S, CLOSE_GRACE = 300, timedelta(minutes=10)
SYNC_EVERY_S = 60


async def close_live_batches() -> int:
    """整点已过 10 分钟的实时批次置为 done。"""
    async with db.session() as s:
        result = await s.execute(
            update(FeatureBatch)
            .where(
                FeatureBatch.mode == "live", FeatureBatch.status == "running",
                FeatureBatch.data_to < now() - CLOSE_GRACE,
            )
            .values(status="done", finished_at=now())
        )
        return result.rowcount


async def sync_subscriptions() -> None:
    """同步全部与 GDE 不一致的 recipe 订阅。"""
    await subscription.sync()


async def _every(seconds: float, fn) -> None:
    while True:
        try:
            await fn()
        except Exception:
            log.exception("定时任务 %s 出错", fn.__name__)
        await asyncio.sleep(seconds)


async def run_forever() -> None:
    """后台循环：定时收尾实时批次、同步 recipe 订阅。"""
    await asyncio.gather(_every(CLOSE_EVERY_S, close_live_batches), _every(SYNC_EVERY_S, sync_subscriptions))
