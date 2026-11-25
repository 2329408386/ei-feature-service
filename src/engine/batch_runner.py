#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""补算 / 重算：领取排队中的批次，经 SDK 拉执行清单，逐个 run_id 计算。

- fill：已有结果的跳过；overwrite：同一事务里先删后插，完成后把被覆盖的旧批次标记 superseded_by。
- 每处理一个 run_id 前检查批次是否被取消。
- 多实例用 FOR UPDATE SKIP LOCKED 领取，同一批次只会被一个实例执行。
"""

import asyncio
import logging

from sqlalchemy import select, text

from src.engine.sandbox import get_sandbox
from src.engine.writer import FeatureRef, RunRef, add_run, has_value, write_value
from src.model.tables import BindingItem, ChamberBinding, Feature, FeatureBatch
from src.service.binding import recipes_of
from src.util import db
from src.util.gde import GdeError, GdeUnavailable, get_gde
from src.util.limits import limits
from src.util.timeutil import now

log = logging.getLogger(__name__)
POLL_S = 5
RETRY_DELAYS_S = (5, 20, 60)  # GDE 不可用时的退避重试间隔


class BatchFailed(Exception):
    pass


async def _with_retry(make_call):
    """GDE 不可用时按 RETRY_DELAYS_S 退避重试，仍失败抛 BatchFailed。"""
    for delay in (*RETRY_DELAYS_S, None):
        try:
            return await make_call()
        except GdeUnavailable as e:
            if delay is None:
                raise BatchFailed(f"GDE 不可用：{e}") from None
            await asyncio.sleep(delay)


async def claim_next() -> FeatureBatch | None:
    """领取最早的排队中批次并置为 running；多实例用 SKIP LOCKED 避免重复领取。"""
    async with db.session() as s:
        stmt = select(FeatureBatch).where(FeatureBatch.status == "queued").order_by(FeatureBatch.batch_id)
        b = (await s.execute(stmt.limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if b:
            b.status, b.started_at = "running", now()
        return b


async def _status(batch_id: int) -> str:
    async with db.session() as s:
        return await s.scalar(select(FeatureBatch.status).where(FeatureBatch.batch_id == batch_id))


async def _finish(batch_id: int, status: str, error: str | None = None) -> None:
    async with db.session() as s:
        b = await s.get(FeatureBatch, batch_id, with_for_update=True)
        if b.status == "running":  # 已被取消的批次保持 cancelled
            b.status, b.error, b.finished_at = status, error, now()


async def _plan(b: FeatureBatch) -> tuple[list[str], dict[str, list[FeatureRef]]]:
    """该批次涉及的配方，以及每个配方要算的特征（限定在批次所选特征内）。"""
    async with db.session() as s:
        rev = await s.get(ChamberBinding, (b.chamber, b.binding_revision))
        rows = (await s.execute(
            select(BindingItem.recipe, Feature.feature_id, Feature.version, Feature.package_hash)
            .join(Feature, (Feature.feature_id == BindingItem.feature_id) & (Feature.version == BindingItem.version))
            .where(BindingItem.chamber == b.chamber, BindingItem.revision == b.binding_revision)
        )).tuples().all()
    plan: dict[str, list[FeatureRef]] = {}
    for recipe, fid, ver, h in rows:
        if f"{fid}@{ver}" in b.features:
            plan.setdefault(recipe, []).append(FeatureRef(fid, ver, h))
    return [r for r in recipes_of(rev.spec) if r in plan], plan


async def _supersede(b: FeatureBatch) -> None:
    """重算完成后：同腔室、时间重叠、特征有交集、尚未被覆盖的旧批次标记为被本批次覆盖。"""
    async with db.session() as s:
        await s.execute(
            text("""
                UPDATE feature_batch SET superseded_by = :bid
                WHERE chamber = :chamber AND batch_id <> :bid AND superseded_by IS NULL
                  AND status IN ('running', 'done') AND data_from < :to AND data_to > :from
                  AND (features IS NULL OR features ?| CAST(:feats AS text[]))
            """),
            {"bid": b.batch_id, "chamber": b.chamber, "from": b.data_from, "to": b.data_to, "feats": b.features},
        )


async def _list_runs(b: FeatureBatch, recipes: list[str]) -> list:
    """经 SDK 拉取批次时间范围内、属于这些配方的执行清单。"""
    if not recipes:
        return []
    gde = get_gde()
    return await _with_retry(lambda: asyncio.to_thread(gde.list_runs, b.chamber, recipes, b.data_from, b.data_to))


async def _compute_one(b: FeatureBatch, f: FeatureRef, ref: RunRef, overwrite: bool) -> None:
    """计算一个 (特征, 执行) 并写入；fill 模式下已有值则跳过。"""
    if not overwrite:
        async with db.session() as s:
            if await has_value(s, f, ref.run_id):
                return

    async def call():
        async with limits.slot(b.chamber, backfill=True):
            return await get_sandbox().run(f.feature_id, f.version, f.package_hash, ref.run_id)

    r = await _with_retry(call)
    async with db.session() as s:
        await write_value(s, b.batch_id, f, ref, r, overwrite=overwrite)


async def execute(b: FeatureBatch) -> None:
    """执行一个补算 / 重算批次：逐个 run_id 计算；每个执行前检查是否已被取消。"""
    recipes, plan = await _plan(b)
    overwrite = b.mode == "overwrite"
    try:
        for run in await _list_runs(b, recipes):
            if await _status(b.batch_id) == "cancelled":
                return
            ref = RunRef(run.run_id, run.tool, run.chamber, run.recipe, run.lot, run.wafer, run.end_at)
            for f in plan.get(run.recipe, []):
                await _compute_one(b, f, ref, overwrite)
            async with db.session() as s:
                await add_run(s, b.batch_id)
    except (BatchFailed, GdeError) as e:
        await _finish(b.batch_id, "failed", str(e))
        return
    if overwrite:
        await _supersede(b)
    await _finish(b.batch_id, "done")


async def run_forever() -> None:
    """后台循环：领取批次并执行，没有排队的批次时休眠 POLL_S 秒。"""
    while True:
        b = await claim_next()
        if b is None:
            await asyncio.sleep(POLL_S)
            continue
        log.info("开始执行批次 #%s（%s %s）", b.batch_id, b.mode, b.chamber)
        try:
            await execute(b)
        except Exception as e:
            log.exception("批次 #%s 执行出错", b.batch_id)
            await _finish(b.batch_id, "failed", repr(e)[:1024])
