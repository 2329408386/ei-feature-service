#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""定时任务：实时批次收尾、补做 gde_outbox 中的 SDK 调用。"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

from src.model.tables import FeatureBatch, GdeOutbox
from src.util import db
from src.util.gde import get_gde
from src.util.timeutil import now

log = logging.getLogger(__name__)
CLOSE_EVERY_S, CLOSE_GRACE = 300, timedelta(minutes=10)
OUTBOX_EVERY_S, OUTBOX_MAX_ATTEMPTS = 60, 20


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


async def process_outbox() -> None:
    """逐条执行未完成的 SDK 调用；失败记录原因，下次再试。"""
    gde = get_gde()
    async with db.session() as s:
        stmt = (
            select(GdeOutbox).where(GdeOutbox.done_at.is_(None), GdeOutbox.attempts < OUTBOX_MAX_ATTEMPTS)
            .order_by(GdeOutbox.id).limit(50).with_for_update(skip_locked=True)
        )
        for job in (await s.execute(stmt)).scalars():
            job.attempts += 1
            try:
                await asyncio.to_thread(getattr(gde, job.action), **job.payload)
                job.done_at = now()
            except Exception as e:
                job.last_error = repr(e)[:1024]
                log.warning("gde_outbox #%s %s 失败（第 %s 次）：%s", job.id, job.action, job.attempts, e)


async def _every(seconds: float, fn) -> None:
    while True:
        try:
            await fn()
        except Exception:
            log.exception("定时任务 %s 出错", fn.__name__)
        await asyncio.sleep(seconds)


async def run_forever() -> None:
    """后台循环：定时收尾实时批次、补做 gde_outbox。"""
    await asyncio.gather(_every(CLOSE_EVERY_S, close_live_batches), _every(OUTBOX_EVERY_S, process_outbox))
