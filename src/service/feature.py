#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征：新增、查询、提交评审、评审、下线、删除草稿。

特征只有元数据（feature.json），计算代码在算子里（见 operator.py）。
同一 feature_id 同时最多一个已发布版本：新版本评审通过时，旧的已发布版本自动下线（其算子不受影响）；
手动下线已发布版本表示整个特征下线，其全部已发布算子随之下线并从绑定中移除。
"""

from sqlalchemy import func, or_, select, text

from src.api.schemas import ReviewIn
from src.model.tables import Feature, Operator
from src.service import operator, subscription
from src.service import review as flow
from src.service.package import check_feature_spec
from src.util import db, semver
from src.util.errors import conflict, invalid, not_found
from src.util.timeutil import now


async def _get(s, feature_id: str, version: str, lock: bool = False) -> Feature:
    stmt = select(Feature).where(Feature.feature_id == feature_id, Feature.version == version)
    row = (await s.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        raise not_found(f"特征 {feature_id}@{version}")
    return row


# ---------------------------------------------------------------- 新增与查询
async def create(spec: dict, user: str) -> Feature:
    """校验 feature.json，版本号必须大于已有版本，生成草稿。"""
    errors = check_feature_spec(spec)
    if errors:
        raise invalid("SPEC_INVALID", "feature.json 校验失败", errors)
    fid, ver = spec["id"], spec["version"]
    async with db.session() as s:
        await db.lock(s, f"feature:{fid}")  # 同一特征的新增串行，版本判断才可靠
        versions = (await s.execute(select(Feature.version).where(Feature.feature_id == fid))).scalars().all()
        if ver in versions:
            raise conflict("VERSION_EXISTS", f"{fid}@{ver} 已存在")
        if versions and semver.parse(ver) <= max(map(semver.parse, versions)):
            raise conflict("VERSION_NOT_INCREASING", f"新版本必须大于已有的最大版本 {max(versions, key=semver.parse)}")
        row = Feature(feature_id=fid, version=ver, status="draft", spec=spec, created_by=user, review_log=[])
        s.add(row)
    return row


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


async def get_feature(feature_id: str, version: str) -> tuple[Feature, list[Operator]]:
    """特征和该 feature_id 下的全部算子（按名称、版本降序）。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version)
        ops = (await s.execute(
            select(Operator).where(Operator.feature_id == feature_id)
            .order_by(Operator.name, Operator.version.desc())
        )).scalars().all()
    return row, ops


# ---------------------------------------------------------------- 状态流转
async def submit(feature_id: str, version: str, user: str) -> Feature:
    """草稿 -> 待评审，只有提交人可以提交。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        flow.submit(row, user)
    return row


async def review(feature_id: str, version: str, body: ReviewIn, user: str) -> Feature:
    """评审（评审人不能是提交人，不需要试跑）。通过时旧的已发布版本自动下线。"""
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, version, lock=True)
        if flow.review(row, body, user, need_trial=False):
            old = (await s.execute(
                select(Feature).where(Feature.feature_id == feature_id, Feature.status == "published").with_for_update()
            )).scalar_one_or_none()
            if old is not None:
                old.status, old.retired_by, old.retired_at = "retired", user, now()
                old.retire_reason = f"被 {version} 替代"
                await s.flush()  # 先让旧版本退出 published，避免违反“每个特征一个已发布版本”的唯一索引
            flow.publish(row, user)
    return row


async def retire(feature_id: str, version: str, reason: str, user: str) -> tuple[Feature, list[str], list[dict]]:
    """下线整个特征：该版本 -> retired，其全部已发布算子一起下线并从绑定中移除。"""
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, version, lock=True)
        flow.retire(row, reason, user)
        ops = (await s.execute(
            select(Operator).where(Operator.feature_id == feature_id, Operator.status == "published")
            .order_by(Operator.name, Operator.version).with_for_update()
        )).scalars().all()
        refs, affected = [], []
        for op in ops:
            refs.append(op.ref)
            hit = await operator.retire_in_tx(s, op, f"特征 {feature_id}@{version} 下线：{reason}", user)
            if hit:
                affected.append(hit)
    await subscription.sync({c for a in affected for c in a["chambers"]})
    return row, refs, affected


async def delete_draft(feature_id: str, version: str, user: str) -> None:
    """删除自己的草稿。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        flow.check_deletable(row, user)
        await s.delete(row)
