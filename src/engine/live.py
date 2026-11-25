#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""实时计算：监听 Redis Stream gde:run:done，每条通知按绑定计算该执行的全部特征。

一条消息的所有特征写完才 XACK；GDE 不可用时不 ACK，等认领后重试；
投递次数超过 MAX_DELIVERIES 时，把还没结果的特征记为 FETCH_ERROR 后 ACK。
"""

import asyncio
import logging
import time
from datetime import datetime

from src.engine.sandbox import RunResult, get_sandbox
from src.engine.writer import (
    FeatureRef,
    RunRef,
    active_revision,
    add_run,
    features_for,
    has_value,
    live_batch,
    write_value,
)
from src.util import db
from src.util.config import MAX_DELIVERIES
from src.util.gde import GdeUnavailable
from src.util.limits import limits
from src.util.stream import RunDoneStream, get_redis

log = logging.getLogger(__name__)
CLAIM_EVERY_S = 60


def parse(fields: dict[str, str]) -> RunRef:
    """通知字段：run_id, tool, chamber, recipe, end_at 必填；wafer, lot, sub_id 可选。"""
    return RunRef(
        run_id=fields["run_id"], tool=fields["tool"], chamber=fields["chamber"], recipe=fields["recipe"],
        lot=fields.get("lot") or None, wafer=fields.get("wafer") or None,
        end_at=datetime.fromisoformat(fields["end_at"]),
    )


class LiveConsumer:
    def __init__(self, stream: RunDoneStream | None = None):
        self.stream = stream or RunDoneStream(get_redis())

    @staticmethod
    async def _compute(f: FeatureRef, run: RunRef, give_up: bool) -> RunResult | None:
        """在沙箱中计算一个特征；超过最大投递次数直接记空值；GDE 不可用返回 None（稍后重试）。"""
        if give_up:
            return RunResult(reason="FETCH_ERROR", reason_detail="超过最大投递次数")
        try:
            async with limits.slot(run.chamber):
                r = await get_sandbox().run(f.feature_id, f.version, f.package_hash, run.run_id)
        except GdeUnavailable as e:
            limits.breaker.failure()
            log.warning("GDE 不可用，%s 稍后重试：%s", run.run_id, e)
            return None
        limits.breaker.success()
        return r

    async def run_forever(self) -> None:
        """持续读取新消息，并定期认领超时未确认的消息。"""
        await self.stream.ensure_group()
        last_claim = 0.0
        while True:
            msgs = await self.stream.read()
            if time.monotonic() - last_claim > CLAIM_EVERY_S:
                last_claim = time.monotonic()
                msgs += await self.stream.claim()
            await asyncio.gather(*(self.handle(msg_id, fields) for msg_id, fields in msgs))

    async def handle(self, msg_id: str, fields: dict[str, str]) -> None:
        """处理一条通知：格式错误直接确认丢弃；处理完毕才确认，否则留待重投。"""
        try:
            run = parse(fields)
        except (KeyError, ValueError) as e:
            log.warning("丢弃格式错误的通知 %s：%r %s", msg_id, fields, e)
            await self.stream.ack(msg_id)
            return
        try:
            if await self.process(run, give_up=await self.stream.deliveries(msg_id) > MAX_DELIVERIES):
                await self.stream.ack(msg_id)
        except Exception:
            log.exception("处理通知 %s 失败，等待重试", msg_id)

    async def process(self, run: RunRef, give_up: bool = False) -> bool:
        """计算这次执行要算的特征。返回 True 表示处理完毕可以 ACK。"""
        async with db.session() as s:
            revision = await active_revision(s, run.chamber)
            feats = await features_for(s, run.chamber, revision, run.recipe) if revision else []
            if not feats:  # 腔室没有生效绑定，或配方不在绑定内
                return True
            batch_id = await live_batch(s, run.chamber, revision, run.end_at)

        wrote = False
        for f in feats:
            async with db.session() as s:
                if await has_value(s, f, run.run_id):  # 重复通知：已算过就跳过
                    continue
            r = await self._compute(f, run, give_up)
            if r is None:
                return False
            async with db.session() as s:
                wrote |= await write_value(s, batch_id, f, run, r)
        if wrote:
            async with db.session() as s:
                await add_run(s, batch_id)
        return True
