#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征：新增、查询、提交评审、评审、下线、重新上线、删除。

特征只有元数据（feature.json），计算代码在算子里（见 operator.py）。
同一 feature_id 同时最多一个已发布版本：新版本评审通过时，旧的已发布版本自动下线（其算子不受影响）；
手动下线已发布版本表示整个特征下线，其全部已发布算子随之下线并从绑定中移除。
"""

import asyncio

from sqlalchemy import delete, func, or_, select, text

from src.api.schemas import ReviewIn
from src.model.tables import Feature, FeatureMeta, Operator
from src.service import meta, operator, subscription
from src.service import review as flow
from src.service.package import FEATURE_FILE, check_feature_spec, name_required
from src.util import db, semver
from src.util.errors import conflict, invalid, invalid_state, not_found
from src.util.storage import get_store, operator_prefix
from src.util.timeutil import now


async def _get(s, feature_id: str, version: str, lock: bool = False) -> Feature:
    stmt = select(Feature).where(Feature.feature_id == feature_id, Feature.version == version)
    row = (await s.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        raise not_found(f"特征 {feature_id}@{version}")
    return row


# ---------------------------------------------------------------- 新增与查询
async def _new_feature_id(s) -> str:
    """后台生成特征 ID：feat_0001、feat_0002 …（序列取号，跳过已被占用的）。"""
    while True:
        fid = f"feat_{await s.scalar(text("SELECT nextval('feature_id_seq')")):04d}"
        if await s.scalar(select(Feature.feature_id).where(Feature.feature_id == fid).limit(1)) is None:
            return fid


async def create(spec: dict, user: str) -> Feature:
    """校验 feature.json，生成草稿。

    不带 id：新特征，后台生成 feature_id，name 必填，version 默认 1.0.0。
    带 id：该特征已存在时为新版本（版本号必须大于已有版本，name 可选，给了就改名）；不存在时按这个 id 新建（供 Agent / 脚本使用，name 同样必填）。
    name 写入 feature_meta，不存进 spec。
    """
    spec = dict(spec)
    if "id" not in spec:
        spec.setdefault("version", "1.0.0")
    errors = check_feature_spec(spec)
    if errors:
        raise invalid("SPEC_INVALID", "feature.json 校验失败", errors)
    name = spec.pop("name", None)
    async with db.session() as s:
        fid = spec.pop("id", None) or await _new_feature_id(s)
        spec = {"id": fid, **spec}
        ver = spec["version"]
        await db.lock(s, f"feature:{fid}")  # 同一特征的新增串行，版本判断才可靠
        versions = (await s.execute(select(Feature.version).where(Feature.feature_id == fid))).scalars().all()
        if not versions and not (name or "").strip():
            raise invalid("SPEC_INVALID", "feature.json 校验失败", [name_required(FEATURE_FILE)])
        if ver in versions:
            raise conflict("VERSION_EXISTS", f"{fid}@{ver} 已存在")
        if versions and semver.parse(ver) <= max(map(semver.parse, versions)):
            raise conflict("VERSION_NOT_INCREASING", f"新版本必须大于已有的最大版本 {max(versions, key=semver.parse)}")
        row = Feature(feature_id=fid, version=ver, status="draft", spec=spec, created_by=user, review_log=[])
        s.add(row)
        if name is not None:
            await meta.upsert_feature_name(s, fid, name, user)
        await meta.attach_features(s, [row])
    return row


async def list_features(status: str | None, q: str | None, feature_id: str | None, tool: str | None,
                        page: int, size: int):
    """分页查询，q 匹配特征 ID、显示名称或描述，tool 按关联机台过滤；按特征 ID、版本号降序。返回 (rows, total)。"""
    cond = []
    if status:
        cond.append(Feature.status == status)
    if feature_id:
        cond.append(Feature.feature_id == feature_id)
    if q:
        like = f"%{q}%"
        cond.append(or_(Feature.feature_id.ilike(like), FeatureMeta.name.ilike(like),
                        Feature.spec["desc"].astext.ilike(like)))
    async with db.session() as s:
        tools = await meta.feature_tools(s)
        if tool:
            cond.append(Feature.feature_id.in_([fid for fid, ts in tools.items() if tool in ts]))
        base = select(Feature).outerjoin(FeatureMeta, FeatureMeta.feature_id == Feature.feature_id).where(*cond)
        total = await s.scalar(select(func.count()).select_from(base.subquery()))
        rows = (
            await s.execute(
                base.order_by(Feature.feature_id, text("string_to_array(feature.version, '.')::int[] DESC"))
                .offset((page - 1) * size).limit(size)
            )
        ).scalars().all()
        await meta.attach_features(s, rows, tools)
    return rows, total


async def get_feature(feature_id: str, version: str) -> tuple[Feature, list[Operator]]:
    """特征和该 feature_id 下的全部算子（按名称、版本降序）。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version)
        ops = (await s.execute(
            select(Operator).where(Operator.feature_id == feature_id)
            .order_by(Operator.name, Operator.version.desc())
        )).scalars().all()
        await meta.attach_features(s, [row])
        await meta.attach_operators(s, ops)
    return row, ops


# ---------------------------------------------------------------- 状态流转
async def submit(feature_id: str, version: str, user: str) -> Feature:
    """草稿 -> 待评审，只有提交人可以提交。"""
    async with db.session() as s:
        row = await _get(s, feature_id, version, lock=True)
        flow.submit(row, user)
        await meta.attach_features(s, [row])
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
        await meta.attach_features(s, [row])
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
        await meta.attach_features(s, [row])
    await subscription.sync({r for a in affected for r in a["recipes"]})
    return row, refs, affected


async def restore(feature_id: str, version: str, comment: str | None, with_operators: bool,
                  user: str) -> tuple[Feature, list[str]]:
    """已下线的特征版本重新上线（不再评审）。同一特征同时只能有一个已发布版本。

    with_operators：把随这次特征下线而下线的算子一并重新上线。绑定不会自动恢复，需要重新提交。
    返回 (特征, 一并上线的算子引用)。
    """
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, version, lock=True)
        if row.status == "retired":
            pub = await s.scalar(select(Feature.version).where(
                Feature.feature_id == feature_id, Feature.status == "published"))
            if pub:
                raise invalid_state(f"{feature_id}@{pub} 已发布，同一特征只能有一个已发布版本；请先下线它", "published")
        flow.restore(row, user, comment)
        restored = []
        if with_operators:
            prefix = f"特征 {feature_id}@{version} 下线"
            ops = (await s.execute(select(Operator).where(
                Operator.feature_id == feature_id, Operator.status == "retired"
            ).order_by(Operator.name, Operator.version).with_for_update())).scalars().all()
            for op in ops:
                if (op.retire_reason or "").startswith(prefix):
                    flow.restore(op, user, f"随特征 {feature_id}@{version} 重新上线")
                    restored.append(op.ref)
        await s.flush()
        await meta.attach_features(s, [row])
    return row, restored


async def remove(feature_id: str, version: str, user: str, dry_run: bool = False) -> dict:
    """删除特征版本：自己的草稿或已下线的（见 review.check_removable）。dry_run 只返回影响范围。

    删除的是该特征最后一个版本时，特征下的全部算子（及其特征值、绑定修订、文件）和名称一并删除；
    这些算子里有已发布或待评审的则拒绝（先下线 / 处理）。不可恢复。
    """
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, version, lock=True)
        flow.check_removable(row, user)
        last = await s.scalar(select(func.count()).select_from(Feature).where(Feature.feature_id == feature_id)) == 1
        ops = []
        if last:
            ops = (await s.execute(select(Operator).where(Operator.feature_id == feature_id)
                                   .order_by(Operator.name, Operator.version).with_for_update())).scalars().all()
            busy = [o.ref for o in ops if o.status in ("published", "review")]
            if busy:
                raise invalid_state(f"特征下还有已发布或待评审的算子：{'、'.join(busy)}，请先下线或退回", "busy")
        out = {"features": [f"{feature_id}@{version}"], "last_version": last, **await operator.impact(s, ops)}
        if dry_run:
            return out
        recipes = await operator.delete_in_tx(s, ops)
        prefixes = [operator_prefix(o.feature_id, o.name, o.version) for o in ops]
        await s.delete(row)
        await s.flush()
        if last:
            await s.execute(delete(FeatureMeta).where(FeatureMeta.feature_id == feature_id))
    await subscription.sync(recipes)
    for p in prefixes:
        await asyncio.to_thread(get_store().delete_prefix, p)
    return out
