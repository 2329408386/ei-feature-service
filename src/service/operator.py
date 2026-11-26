#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子：新增（或同名算子的新版本）、查询、提交评审、评审、下线、删除草稿、试跑。

- 算子挂在 feature_id 下，新增算子不改变特征版本；同名算子再次提交时版本号自动 +1（1, 2, 3 …）。
- 代码文件名由用户指定，不指定时为 {name}_v{version}.py；同一特征内文件名不能重复。
- 评审通过需至少试跑 1 个 run_id，且所属特征已有已发布版本。
"""

import asyncio
import json

from sqlalchemy import delete, func, or_, select, tuple_

from src.api.schemas import OperatorIn, OperatorQuery, ReviewIn
from src.engine.sandbox import get_sandbox
from src.model.tables import BindingItem, Feature, FeatureValue, Operator, OperatorBinding, OperatorMeta
from src.service import binding, meta, subscription
from src.service import review as flow
from src.service.package import OPERATOR_FILE, check_operator, name_required
from src.util import db
from src.util.config import TRIAL_CONCURRENCY
from src.util.errors import conflict, gde_unavailable, invalid, invalid_state, not_found
from src.util.gde import GdeNotFound, GdeUnavailable, get_gde
from src.util.storage import SPEC_FILE, code_hash, get_store, operator_prefix, operator_uri

_trials = asyncio.Semaphore(TRIAL_CONCURRENCY)


async def _get(s, feature_id: str, name: str, version: int, lock: bool = False) -> Operator:
    stmt = select(Operator).where(Operator.feature_id == feature_id, Operator.name == name, Operator.version == version)
    row = (await s.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if row is None:
        raise not_found(f"算子 {feature_id}/{name}@{version}")
    return row


def spec_bytes(spec: dict) -> bytes:
    """operator.json 的规范化字节（键排序），存储和计算 hash 都用它。"""
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


async def _feature_statuses(s, feature_id: str) -> list[str]:
    return (await s.execute(select(Feature.status).where(Feature.feature_id == feature_id))).scalars().all()


# ---------------------------------------------------------------- 新增与查询
async def _new_operator_id(s, feature_id: str) -> str:
    """后台生成算子 ID：特征内 op_1、op_2 …，取第一个未被占用的（调用方已持有 feature 锁）。"""
    used = set((await s.execute(select(Operator.name).where(Operator.feature_id == feature_id).distinct())).scalars())
    n = len(used) + 1
    while f"op_{n}" in used:
        n += 1
    return f"op_{n}"


async def _check_feature_open(s, feature_id: str) -> None:
    """新增算子前检查特征：必须存在，且不能所有版本都已下线。"""
    statuses = await _feature_statuses(s, feature_id)
    if not statuses:
        raise not_found(f"特征 {feature_id}")
    if all(x == "retired" for x in statuses):
        raise invalid_state(f"特征 {feature_id} 已下线，不能再增加算子", "retired")


async def _upload(store, prefix: str, spec: dict, code_file: str, code: str) -> str:
    """把 operator.json 和代码存入对象存储，返回两者的 hash。"""
    spec_raw, code_raw = spec_bytes(spec), code.encode("utf-8")
    await asyncio.to_thread(store.put, prefix + SPEC_FILE, spec_raw)
    await asyncio.to_thread(store.put, prefix + code_file, code_raw)
    return code_hash(spec_raw, code_raw)


async def create(body: OperatorIn, user: str) -> Operator:
    """校验后把 operator.json 和代码存入对象存储，生成草稿。

    operator.json 不带 id：新算子，后台生成算子 ID（op_1、op_2 …，特征内唯一），name（名称）必填。
    带 id：该算子已存在时为新版本（版本号 = 已有最大版本 + 1，name 可选，给了就改名）；不存在时按这个 id 新建（name 同样必填）。
    name 写入 operator_meta，不存进 operator.json。
    """
    errors = check_operator(body.spec, body.code, body.code_file)
    if errors:
        raise invalid("SPEC_INVALID", "算子校验失败", errors)
    fid, spec = body.feature_id, dict(body.spec)
    display = spec.pop("name", None)
    store, prefix = get_store(), None
    try:
        async with db.session() as s:
            await db.lock(s, f"feature:{fid}")  # 同一特征内的算子 ID、版本号、文件名判断串行
            await _check_feature_open(s, fid)
            name = spec.pop("id", None) or await _new_operator_id(s, fid)
            spec = {"id": name, **spec}
            last = await s.scalar(select(func.max(Operator.version)).where(
                Operator.feature_id == fid, Operator.name == name))
            if last is None and not (display or "").strip():
                raise invalid("SPEC_INVALID", "算子校验失败", [name_required(OPERATOR_FILE)])
            ver = (last or 0) + 1
            code_file = body.code_file or f"{name}_v{ver}.py"
            if await s.scalar(select(Operator.name).where(Operator.feature_id == fid, Operator.code_file == code_file)):
                raise conflict("CODE_FILE_EXISTS", f"特征 {fid} 中已有文件 {code_file}，请换一个文件名")
            prefix = operator_prefix(fid, name, ver)
            digest = await _upload(store, prefix, spec, code_file, body.code)
            row = Operator(
                feature_id=fid, name=name, version=ver, status="draft", spec=spec, code_file=code_file,
                code_uri=operator_uri(fid, name, ver), code_hash=digest,
                created_by=user, review_log=[],
            )
            s.add(row)
            if display is not None:
                await meta.upsert_operator_name(s, fid, name, display, user)
            await meta.attach_operators(s, [row])
        return row
    except Exception:
        if prefix:  # 入库失败时清理已上传的文件
            await asyncio.to_thread(store.delete_prefix, prefix)
        raise


async def list_operators(query: OperatorQuery):
    """分页查询，q 匹配算子名、显示名称或描述；按特征、算子名、版本降序。返回 (rows, total)。"""
    feature_id, name, status, q = query.feature_id, query.name, query.status, query.q
    page, size = query.page, query.page_size
    cond = []
    if feature_id:
        cond.append(Operator.feature_id == feature_id)
    if name:
        cond.append(Operator.name == name)
    if status:
        cond.append(Operator.status == status)
    if q:
        like = f"%{q}%"
        cond.append(or_(Operator.name.ilike(like), OperatorMeta.name.ilike(like),
                        Operator.spec["desc"].astext.ilike(like)))
    base = select(Operator).outerjoin(
        OperatorMeta, (OperatorMeta.feature_id == Operator.feature_id) & (OperatorMeta.operator == Operator.name)
    ).where(*cond)
    async with db.session() as s:
        total = await s.scalar(select(func.count()).select_from(base.subquery()))
        rows = (await s.execute(
            base.order_by(Operator.feature_id, Operator.name, Operator.version.desc())
            .offset((page - 1) * size).limit(size)
        )).scalars().all()
        await meta.attach_operators(s, rows)
    return rows, total


async def get_operator(feature_id: str, name: str, version: int) -> tuple[Operator, str]:
    """算子和它的代码。"""
    async with db.session() as s:
        row = await _get(s, feature_id, name, version)
        await meta.attach_operators(s, [row])
    code = await asyncio.to_thread(get_store().get, operator_prefix(feature_id, name, version) + row.code_file)
    return row, code.decode("utf-8")


# ---------------------------------------------------------------- 状态流转
async def submit(feature_id: str, name: str, version: int, user: str) -> Operator:
    """草稿 -> 待评审，只有提交人可以提交。"""
    async with db.session() as s:
        row = await _get(s, feature_id, name, version, lock=True)
        flow.submit(row, user)
        await meta.attach_operators(s, [row])
    return row


async def review(feature_id: str, name: str, version: int, body: ReviewIn, user: str) -> Operator:
    """评审：通过需至少试跑 1 个 run_id，且所属特征已发布；退回需填写意见。"""
    async with db.session() as s:
        row = await _get(s, feature_id, name, version, lock=True)
        if flow.review(row, body, user, need_trial=True):
            if "published" not in await _feature_statuses(s, feature_id):
                raise invalid_state(f"特征 {feature_id} 还没有已发布版本，算子不能先发布", "unpublished")
            flow.publish(row, user)
        await meta.attach_operators(s, [row])
    return row


async def retire_in_tx(s, row: Operator, reason: str, user: str) -> dict | None:
    """在调用方事务里下线算子并从绑定中移除；返回受影响的绑定（见 binding.remove_operator）。"""
    flow.retire(row, reason, user)
    return await binding.remove_operator(s, row.feature_id, row.name, row.version)


async def retire(feature_id: str, name: str, version: int, reason: str, user: str) -> tuple[Operator, list[dict]]:
    """下线算子：从生效绑定中移除（生成 system 修订），撤回引用它的待审核修订，提交后同步 GDE 订阅。"""
    async with db.session() as s:
        row = await _get(s, feature_id, name, version, lock=True)
        hit = await retire_in_tx(s, row, reason, user)
        await meta.attach_operators(s, [row])
    affected = [hit] if hit else []
    await subscription.sync({r for a in affected for r in a["recipes"]})
    return row, affected


async def restore(feature_id: str, name: str, version: int, comment: str | None, user: str) -> Operator:
    """已下线的算子重新上线（不再评审）。特征必须有已发布版本；绑定不会自动恢复，需要重新提交。"""
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, name, version, lock=True)
        if row.status == "retired" and "published" not in await _feature_statuses(s, feature_id):
            raise invalid_state(f"特征 {feature_id} 没有已发布版本，请先让特征重新上线", "unpublished")
        flow.restore(row, user, comment)
        await meta.attach_operators(s, [row])
    return row


async def impact(s, rows: list[Operator]) -> dict:
    """删除这些算子版本会删掉什么：特征值条数；某个算子一个版本都不剩时，它的绑定修订数。"""
    keys = [(r.feature_id, r.name, r.version) for r in rows]
    values = 0
    if keys:
        values = await s.scalar(select(func.count()).select_from(FeatureValue).where(
            tuple_(FeatureValue.feature_id, FeatureValue.operator, FeatureValue.version).in_(keys)))
    gone = await _gone_operators(s, rows)
    bindings = 0
    if gone:
        bindings = await s.scalar(select(func.count()).select_from(OperatorBinding).where(
            tuple_(OperatorBinding.feature_id, OperatorBinding.operator).in_(gone)))
    return {"operators": [r.ref for r in rows], "values": values, "bindings": bindings,
            "removed_operators": [f"{fid}/{name}" for fid, name in sorted(gone)]}


async def _gone_operators(s, rows: list[Operator]) -> set[tuple[str, str]]:
    """删掉 rows 后一个版本都不剩的 (feature_id, 算子名)。"""
    out = set()
    for fid, name in {(r.feature_id, r.name) for r in rows}:
        versions = set((await s.execute(select(Operator.version).where(
            Operator.feature_id == fid, Operator.name == name))).scalars())
        if versions <= {r.version for r in rows if (r.feature_id, r.name) == (fid, name)}:
            out.add((fid, name))
    return out


async def delete_in_tx(s, rows: list[Operator]) -> set[str]:
    """在调用方事务里删除这些算子版本：特征值、引用它们的绑定明细、算子行。

    算子一个版本都不剩时再删它的全部绑定修订和名称。返回涉及的 recipe（调用方提交后同步订阅）。"""
    gone = await _gone_operators(s, rows)
    recipes: set[str] = set()
    for r in rows:
        key = (r.feature_id, r.name, r.version)
        await s.execute(delete(FeatureValue).where(
            tuple_(FeatureValue.feature_id, FeatureValue.operator, FeatureValue.version) == key))
        recipes |= set((await s.execute(delete(BindingItem).where(
            tuple_(BindingItem.feature_id, BindingItem.operator, BindingItem.version) == key
        ).returning(BindingItem.recipe))).scalars())
        await s.delete(r)
    await s.flush()
    for fid, name in gone:
        await s.execute(delete(BindingItem).where(BindingItem.feature_id == fid, BindingItem.operator == name))
        await s.execute(delete(OperatorBinding).where(
            OperatorBinding.feature_id == fid, OperatorBinding.operator == name))
        await s.execute(delete(OperatorMeta).where(OperatorMeta.feature_id == fid, OperatorMeta.operator == name))
    await subscription.refresh(s, recipes)
    return recipes


async def remove(feature_id: str, name: str, version: int, user: str, dry_run: bool = False) -> dict:
    """删除算子版本：自己的草稿或已下线的（见 review.check_removable）。dry_run 只返回影响范围。

    同时删除该版本的特征值和对象存储里的文件；算子一个版本都不剩时，其绑定修订和名称一并删除。不可恢复。
    """
    async with db.session() as s:
        await db.lock(s, f"feature:{feature_id}")
        row = await _get(s, feature_id, name, version, lock=True)
        flow.check_removable(row, user)
        out = await impact(s, [row])
        if dry_run:
            return out
        recipes = await delete_in_tx(s, [row])
    await subscription.sync(recipes)
    await asyncio.to_thread(get_store().delete_prefix, operator_prefix(feature_id, name, version))
    return out


# ---------------------------------------------------------------- 试跑
async def trial(feature_id: str, name: str, version: int, run_id: str) -> dict:
    """按生产同样的方式执行一次 fetch + compute，结果只返回，不写库。"""
    from src.engine.writer import OperatorRef

    async with db.session() as s:
        row = await _get(s, feature_id, name, version)
    if row.status == "retired":
        raise invalid_state("已下线的算子不能试跑", row.status)
    try:
        run = await asyncio.to_thread(get_gde().run_info, run_id)
        async with _trials:
            r = await get_sandbox().run(OperatorRef.of(row), run_id)
    except GdeNotFound:
        raise not_found(f"执行 {run_id}") from None
    except GdeUnavailable as e:
        raise gde_unavailable(str(e)) from None
    run_out = {k: getattr(run, k) for k in ("run_id", "tool", "chamber", "recipe", "lot", "wafer", "end_at")}
    return {"run": run_out, **r.__dict__}
