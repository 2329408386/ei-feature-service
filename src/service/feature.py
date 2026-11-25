#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征：上传、查询、提交评审、评审、下线、删除草稿、试跑。

状态流转：draft --submit(本人)--> review --approve(他人)--> published --retire--> retired
                                  review --reject(他人, 意见)--> draft
"""

import asyncio

from sqlalchemy import func, or_, select, text

from src.api.schemas import FeatureReviewIn
from src.engine.sandbox import get_sandbox
from src.model.tables import Feature
from src.service import binding
from src.service.package import check_package
from src.util import db, semver
from src.util.config import TRIAL_CONCURRENCY
from src.util.errors import conflict, forbidden, gde_unavailable, invalid, invalid_state, not_found
from src.util.gde import GdeNotFound, GdeUnavailable, get_gde
from src.util.storage import CODE_FILE, SPEC_FILE, get_store, package_hash, package_uri
from src.util.timeutil import now

_trials = asyncio.Semaphore(TRIAL_CONCURRENCY)


async def _get(s, feature_id: str, version: str, lock: bool = False) -> Feature:
    stmt = select(Feature).where(Feature.feature_id == feature_id, Feature.version == version)
    row = (await s.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        raise not_found(f"特征 {feature_id}@{version}")
    return row


# ---------------------------------------------------------------- 上传与查询
async def upload(spec_raw: bytes, code_raw: bytes, user: str) -> Feature:
    """校验特征包，版本号必须大于已有版本；文件存入对象存储后生成草稿。"""
    spec, errors = check_package(spec_raw, code_raw)
    if errors:
        raise invalid("PACKAGE_INVALID", "特征包校验失败", errors)
    fid, ver = spec["id"], spec["version"]
    store, stored = get_store(), False
    try:
        async with db.session() as s:
            await db.lock(s, f"feature:{fid}")  # 同一特征的上传串行，版本判断才可靠
            versions = (await s.execute(select(Feature.version).where(Feature.feature_id == fid))).scalars().all()
            if ver in versions:
                raise conflict("VERSION_EXISTS", f"{fid}@{ver} 已存在")
            if versions and semver.parse(ver) <= max(map(semver.parse, versions)):
                raise conflict("VERSION_NOT_INCREASING", f"新版本必须大于已有的最大版本 {max(versions, key=semver.parse)}")
            await asyncio.to_thread(store.put, fid, ver, SPEC_FILE, spec_raw)
            await asyncio.to_thread(store.put, fid, ver, CODE_FILE, code_raw)
            stored = True
            row = Feature(
                feature_id=fid, version=ver, status="draft", spec=spec, created_by=user, review_log=[],
                package_uri=package_uri(fid, ver), package_hash=package_hash(spec_raw, code_raw),
            )
            s.add(row)
        return row
    except Exception:
        if stored:  # 入库失败时清理已上传的文件
            await asyncio.to_thread(store.delete, fid, ver)
        raise


async def list_features(status: str | None, q: str | None, feature_id: str | None, page: int, size: int):
    """分页查询，q 匹配特征 ID 或描述；按特征 ID、版本号降序排列。返回 (rows, total)。"""
    cond = []
    if status:
        cond.append(Feature.status == status)
    if feature_id:
        cond.append(Feature.feature_id == feature_id)
    if q:
        like = f"%{q}%"
        cond.append(or_(Feature.feature_id.ilike(like), Feature.spec["desc"].astext.ilike(like)))
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(Feature).where(*cond))
        rows = (
            await s.execute(
                select(Feature).where(*cond)
                .order_by(Feature.feature_id, text("string_to_array(version, '.')::int[] DESC"))
                .offset((page - 1) * size).limit(size)
            )
        ).scalars().all()
    return rows, total


async def get_feature(feature_id: str, version: str) -> tuple[Feature, str]:
    """特征和它的 compute.py 源码。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version)
    code = await asyncio.to_thread(get_store().get, feature_id, version, CODE_FILE)
    return row, code.decode("utf-8")


# ---------------------------------------------------------------- 状态流转
async def submit(feature_id: str, version: str, user: str) -> Feature:
    """草稿 -> 待评审，只有上传人可以提交。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        if row.status != "draft":
            raise invalid_state("只有草稿可以提交评审", row.status)
        if row.created_by != user:
            raise forbidden("只有提交人可以提交评审")
        row.status, row.submitted_at = "review", now()
    return row


async def review(feature_id: str, version: str, body: FeatureReviewIn, user: str) -> Feature:
    """评审（评审人不能是提交人）：通过需至少试跑过一个 run_id，退回需填写意见。"""
    decision, trial_runs = body.decision, body.trial_runs
    comment = (body.comment or "").strip() or None
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        if row.status != "review":
            raise invalid_state("只有待评审的特征可以评审", row.status)
        if row.created_by == user:
            raise forbidden("评审人不能是提交人")
        if decision == "approve":
            if not trial_runs:
                raise invalid("TRIAL_REQUIRED", "评审通过前至少试跑 1 个 run_id")
            row.status, row.published_by, row.published_at = "published", user, now()
        else:
            if not comment:
                raise invalid("VALIDATION_ERROR", "退回时必须填写评审意见")
            row.status = "draft"
        entry = {
            "decision": decision, "reviewer": user, "comment": comment, "trial_runs": trial_runs,
            "at": now().isoformat(),
        }
        row.review_log = [*row.review_log, entry]  # 重新赋值，SQLAlchemy 才会检测到 JSONB 变化
    return row


async def retire(feature_id: str, version: str, reason: str, user: str) -> tuple[Feature, list[dict]]:
    """下线：从所有生效绑定中移除，撤回相关待审核修订；订阅变更写入 gde_outbox 后立即尝试执行。"""
    from src.engine.jobs import process_outbox

    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        if row.status != "published":
            raise invalid_state("只有已发布的特征可以下线", row.status)
        row.status, row.retired_by, row.retired_at, row.retire_reason = "retired", user, now(), reason
        affected = await binding.remove_feature(s, feature_id, version)
    await process_outbox()  # 失败的调用留在 outbox，由后台任务继续重试
    return row, affected


async def delete_draft(feature_id: str, version: str, user: str) -> None:
    """删除自己的草稿，连同对象存储中的文件。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        if row.status != "draft":
            raise invalid_state("只有草稿可以删除", row.status)
        if row.created_by != user:
            raise forbidden("只能删除自己的草稿")
        await s.delete(row)
    await asyncio.to_thread(get_store().delete, feature_id, version)


# ---------------------------------------------------------------- 试跑
async def trial(feature_id: str, version: str, run_id: str) -> dict:
    """按生产同样的方式执行一次 fetch + compute，结果只返回，不写库。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version)
    if row.status == "retired":
        raise invalid_state("已下线的特征不能试跑", row.status)
    try:
        run = await asyncio.to_thread(get_gde().run_info, run_id)
        async with _trials:
            r = await get_sandbox().run(feature_id, version, row.package_hash, run_id)
    except GdeNotFound:
        raise not_found(f"执行 {run_id}") from None
    except GdeUnavailable as e:
        raise gde_unavailable(str(e)) from None
    run_out = {k: getattr(run, k) for k in ("run_id", "tool", "chamber", "recipe", "lot", "wafer", "end_at")}
    return {"run": run_out, **r.__dict__}
