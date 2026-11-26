#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子绑定：校验、提交、审核、撤回、查询、导入导出，以及算子下线时的自动修改。

绑定以算子为单位：一个算子（feature_id + 名称）一套修订，修订内容是
{feature_id, operator, version, recipes: [recipe]}，即“该算子版本在哪些 recipe 的执行上计算”。
不选机台 / 腔室：GDE 按 recipe 订阅，自动推送该 recipe 在所有机台 / 腔室上的执行，以后新增的腔室也会推送。

修订状态：(新建) -> pending --approve(他人)--> active（原 active -> superseded）
                   pending --reject(他人)--> rejected / --withdraw(本人)--> withdrawn
source=system 的修订（算子下线时生成）直接以 active 插入。
修订生效 / 被替换后，涉及 recipe 的订阅在同一事务里重算，提交后同步到 GDE（见 subscription.py）。
"""

import asyncio

from pydantic import ValidationError
from sqlalchemy import func, select

from src.api.schemas import EXPORT_FORMAT, BindingSpec
from src.model.tables import BindingItem, Operator, OperatorBinding
from src.service import subscription
from src.util import db
from src.util.auth import SYSTEM
from src.util.errors import (
    AppError,
    conflict,
    error_list,
    forbidden,
    gde_unavailable,
    invalid,
    invalid_state,
    not_found,
)
from src.util.gde import GdeError, GdeNotFound, get_gde
from src.util.timeutil import now


# ---------------------------------------------------------------- 小工具
def recipes_of(spec: dict) -> set[str]:
    """spec 中的全部 recipe。"""
    return set(spec["recipes"])


def normalize(spec: BindingSpec) -> dict:
    """规范化：recipe 去掉首尾空白后排序去重。"""
    return {
        "feature_id": spec.feature_id,
        "operator": spec.operator,
        "version": spec.version,
        "recipes": sorted({r.strip() for r in spec.recipes}),
    }


async def _gde(fn, *args):
    """在线程里调用 GDE；失败转成 502。"""
    try:
        return await asyncio.to_thread(fn, *args)
    except GdeNotFound:
        raise
    except GdeError as e:
        raise gde_unavailable(str(e)) from None


async def _revision(s, feature_id: str, operator: str, status: str) -> OperatorBinding | None:
    stmt = select(OperatorBinding).where(
        OperatorBinding.feature_id == feature_id, OperatorBinding.operator == operator, OperatorBinding.status == status
    )
    return (await s.execute(stmt.with_for_update())).scalar_one_or_none()


async def active(s, feature_id: str, operator: str) -> OperatorBinding | None:
    """算子的生效修订（加行锁），没有则返回 None。"""
    return await _revision(s, feature_id, operator, "active")


async def pending(s, feature_id: str, operator: str) -> OperatorBinding | None:
    """算子的待审核修订（加行锁），没有则返回 None。"""
    return await _revision(s, feature_id, operator, "pending")


def _lock_key(feature_id: str, operator: str) -> str:
    return f"binding:{feature_id}/{operator}"


# ---------------------------------------------------------------- 校验
async def validate(s, spec: BindingSpec) -> tuple[dict, list[dict]]:
    """绑定校验（提交、导入共用）：返回 (规范化后的 spec, 全部错误)。"""
    errors = await _check_operator(s, spec)
    errors += _check_structure(spec)
    errors += await _check_recipes(spec)
    return normalize(spec), errors


async def _check_operator(s, spec: BindingSpec) -> list[dict]:
    """绑定的算子版本必须存在且已发布。"""
    status = await s.scalar(select(Operator.status).where(
        Operator.feature_id == spec.feature_id, Operator.name == spec.operator, Operator.version == spec.version))
    if status == "published":
        return []
    ref = f"{spec.feature_id}/{spec.operator}@{spec.version}"
    return [{"path": "version", "message": f"算子 {ref} 不存在或未发布"}]


def _check_structure(spec: BindingSpec) -> list[dict]:
    """至少一个 recipe，recipe 非空且不重复。"""
    errors = []
    if not spec.recipes:
        errors.append({"path": "recipes", "message": "至少选择一个 recipe"})
    seen: set[str] = set()
    for i, r in enumerate(spec.recipes):
        name = r.strip()
        if not name:
            errors.append({"path": f"recipes[{i}]", "message": "recipe 不能为空"})
        elif name in seen:
            errors.append({"path": f"recipes[{i}]", "message": f"recipe {name} 重复"})
        seen.add(name)
    return errors


async def _check_recipes(spec: BindingSpec) -> list[dict]:
    """经 GDE 反查校验每个 recipe 都存在（至少有一个机台 / 腔室跑过）。"""
    gde, errors = get_gde(), []
    for i, r in enumerate(spec.recipes):
        if not r.strip():
            continue
        try:
            await _gde(gde.recipe_chambers, r.strip())
        except GdeNotFound:
            errors.append({"path": f"recipes[{i}]", "message": f"recipe {r.strip()} 不存在"})
    return errors


async def _check_state(s, feature_id: str, operator: str, spec: dict) -> None:
    """已有待审核 → PENDING_EXISTS；与生效修订一致 → NO_CHANGE。"""
    p = await pending(s, feature_id, operator)
    if p:
        raise conflict("PENDING_EXISTS", f"算子 {feature_id}/{operator} 已有待审核修订 {p.revision}")
    a = await active(s, feature_id, operator)
    if a and a.spec == spec:
        raise conflict("NO_CHANGE", f"与生效修订 {a.revision} 相同")


async def _insert(s, spec: dict, *, status: str, source: str, user: str, base_revision: int | None,
                  **extra) -> OperatorBinding:
    """新增修订（修订号 = 最大值 + 1），并在同一事务里展开写 binding_item。调用方须已持有算子绑定锁。"""
    fid, op = spec["feature_id"], spec["operator"]
    last = await s.scalar(select(func.max(OperatorBinding.revision)).where(
        OperatorBinding.feature_id == fid, OperatorBinding.operator == op))
    row = OperatorBinding(
        feature_id=fid, operator=op, revision=(last or 0) + 1, version=spec["version"], spec=spec, status=status,
        source=source, base_revision=base_revision, created_by=user, **extra,
    )
    s.add(row)
    await s.flush()
    s.add_all(
        BindingItem(feature_id=fid, operator=op, revision=row.revision, recipe=r, version=spec["version"])
        for r in spec["recipes"]
    )
    await s.flush()
    return row


# ---------------------------------------------------------------- 提交、审核、撤回
async def submit(feature_id: str, operator: str, body: BindingSpec, base_revision: int | None, user: str,
                 source: str) -> OperatorBinding:
    """校验并生成待审核修订。同一算子同时只能有一个待审核修订。"""
    if (body.feature_id, body.operator) != (feature_id, operator):
        raise invalid("BINDING_INVALID", "绑定校验失败",
                      [{"path": "operator", "message": "与路径中的 feature_id / operator 不一致"}])
    async with db.session() as s:
        await db.lock(s, _lock_key(feature_id, operator))
        spec, errors = await validate(s, body)
        if errors:
            raise invalid("BINDING_INVALID", "绑定校验失败", errors)
        await _check_state(s, feature_id, operator, spec)
        return await _insert(s, spec, status="pending", source=source, user=user, base_revision=base_revision)


async def _get_revision(s, feature_id: str, operator: str, revision: int) -> OperatorBinding:
    stmt = select(OperatorBinding).where(
        OperatorBinding.feature_id == feature_id, OperatorBinding.operator == operator,
        OperatorBinding.revision == revision,
    )
    row = (await s.execute(stmt.with_for_update())).scalar_one_or_none()
    if row is None:
        raise not_found(f"算子 {feature_id}/{operator} 的修订 {revision}")
    return row


async def review(feature_id: str, operator: str, revision: int, decision: str, comment: str | None,
                 user: str) -> OperatorBinding:
    """审核。通过时原生效修订 -> superseded，重算涉及 recipe 的订阅；提交后同步 GDE（失败由后台任务重试）。"""
    comment = (comment or "").strip() or None
    changed: set[str] = set()
    async with db.session() as s:
        await db.lock(s, _lock_key(feature_id, operator))
        p = await _get_revision(s, feature_id, operator, revision)
        if p.status != "pending":
            raise invalid_state("只有待审核的修订可以审核", p.status)
        if p.created_by == user:
            raise forbidden("审核人不能是提交人")
        if decision == "reject" and not comment:
            raise invalid("VALIDATION_ERROR", "退回时必须填写审核意见")
        p.reviewed_by, p.reviewed_at, p.review_note = user, now(), comment
        if decision == "reject":
            p.status = "rejected"
            return p
        op_status = await s.scalar(select(Operator.status).where(
            Operator.feature_id == feature_id, Operator.name == operator, Operator.version == p.version))
        if op_status != "published":
            raise invalid_state(f"算子 {feature_id}/{operator}@{p.version} 已不是发布状态", op_status or "none")
        a = await active(s, feature_id, operator)
        if a:
            a.status, a.superseded_at = "superseded", now()
            await s.flush()  # 先让旧修订退出 active，避免违反“每个算子一个 active”的唯一索引
            changed |= recipes_of(a.spec)
        p.status = "active"
        changed |= recipes_of(p.spec)
        await s.flush()
        await subscription.refresh(s, changed)
    await subscription.sync(changed)
    return p


async def withdraw(feature_id: str, operator: str, revision: int, user: str) -> OperatorBinding:
    """提交人撤回自己的待审核修订。"""
    async with db.session() as s:
        p = await _get_revision(s, feature_id, operator, revision)
        if p.status != "pending":
            raise invalid_state("只有待审核的修订可以撤回", p.status)
        if p.created_by != user:
            raise forbidden("只有提交人可以撤回")
        p.status = "withdrawn"
    return p


# ---------------------------------------------------------------- 查询与导出
async def list_bindings(feature_id=None, operator=None, recipe=None, status=None) -> list[dict]:
    """每个算子一项：{feature_id, operator, active, pending}。recipe 按生效修订的 binding_item 过滤。"""
    cond = [OperatorBinding.status.in_(["active", "pending"])]
    if feature_id:
        cond.append(OperatorBinding.feature_id == feature_id)
    if operator:
        cond.append(OperatorBinding.operator == operator)
    async with db.session() as s:
        stmt = select(OperatorBinding).where(*cond).order_by(OperatorBinding.feature_id, OperatorBinding.operator)
        rows = (await s.execute(stmt)).scalars().all()
        matched = None
        if recipe:
            q = subscription.active_items(BindingItem.feature_id, BindingItem.operator).where(BindingItem.recipe == recipe)
            matched = set((await s.execute(q)).tuples().all())

    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.feature_id, r.operator)
        item = out.setdefault(key, {"feature_id": r.feature_id, "operator": r.operator, "active": None, "pending": None})
        item[r.status] = r
    return [
        x for k, x in out.items()
        if (status is None or x[status] is not None) and (matched is None or k in matched)
    ]


async def get_revisions(feature_id: str, operator: str, revision: int | None = None):
    """不带 revision 返回算子全部修订（新的在前）；带 revision 返回单个修订。"""
    async with db.session() as s:
        if revision is not None:
            return await _get_revision(s, feature_id, operator, revision)
        stmt = select(OperatorBinding).where(
            OperatorBinding.feature_id == feature_id, OperatorBinding.operator == operator,
        ).order_by(OperatorBinding.revision.desc())
        return (await s.execute(stmt)).scalars().all()


async def export(**filters) -> dict:
    """导出生效修订（不含空修订），格式可以直接再导入。"""
    items = await list_bindings(**filters)
    return {
        "format": EXPORT_FORMAT,
        "exported_at": now().isoformat(),
        "bindings": [
            {**a.spec, "revision": a.revision}
            for a in (x["active"] for x in items)
            if a and a.spec["recipes"]
        ],
    }


# ---------------------------------------------------------------- 导入
async def import_bindings(bindings: list[dict], dry_run: bool, user: str) -> list[dict]:
    """逐个算子校验：new / skip / conflict / error。dry_run=False 时为所有 new 生成待审核修订（同一事务）。"""
    results, seen = [], set()
    async with db.session() as s:
        for raw in bindings:
            results.append(await _import_one(s, raw, seen, dry_run, user))
    return results


async def _import_one(s, raw: dict, seen: set, dry_run: bool, user: str) -> dict:
    """在调用方的事务里处理导入文件中的一个算子，返回该算子的结果项；seen 记录已出现的算子。"""
    fid, op = raw.get("feature_id"), raw.get("operator")
    res = {"feature_id": fid, "operator": op, "action": "error", "message": ""}
    try:
        spec = BindingSpec.model_validate(raw)
    except ValidationError as e:
        res["message"], res["errors"] = "格式错误", error_list(e)
        return res
    if (fid, op) in seen:
        res["message"] = "文件中重复出现"
        return res
    seen.add((fid, op))

    await db.lock(s, _lock_key(fid, op))
    normalized, errors = await validate(s, spec)
    if errors:
        res["message"], res["errors"] = "校验失败", errors
        return res
    try:
        await _check_state(s, fid, op, normalized)
    except AppError as e:
        res["action"] = "conflict" if e.code == "PENDING_EXISTS" else "skip"
        res["message"] = e.message
        return res

    a = await active(s, fid, op)
    res["action"], res["message"] = "new", "生成待审核修订"
    if raw.get("revision") and a and raw["revision"] != a.revision:
        res["message"] += f"（文件基于修订 {raw['revision']}，当前生效修订 {a.revision}）"
    if not dry_run:
        row = await _insert(
            s, normalized, status="pending", source="import", user=user, base_revision=a.revision if a else None,
        )
        res["revision"] = row.revision
    return res


# ---------------------------------------------------------------- 算子下线
async def remove_operator(s, feature_id: str, operator: str, version: int) -> dict | None:
    """在调用方的事务里：撤回引用该算子版本的待审核修订；生效修订是该版本时，插入一个空的 system 修订直接生效。

    返回 {feature_id, operator, version, new_revision, recipes}；什么都没动返回 None。
    涉及 recipe 的订阅在本事务里重算，调用方提交后再调 subscription.sync。
    """
    await db.lock(s, _lock_key(feature_id, operator))
    ref = f"{feature_id}/{operator}@{version}"
    p = await pending(s, feature_id, operator)
    withdrew = p is not None and p.version == version
    if withdrew:
        p.status, p.review_note = "withdrawn", f"算子 {ref} 下线，自动撤回"

    a = await active(s, feature_id, operator)
    if a is None or a.version != version:
        return {"feature_id": feature_id, "operator": operator, "version": version, "new_revision": None,
                "recipes": []} if withdrew else None
    if p is not None and not withdrew:  # 待审核修订基于旧的生效修订，也一并撤回
        p.status, p.review_note = "withdrawn", f"算子 {ref} 下线，自动撤回，请基于新修订重新提交"
    a.status, a.superseded_at = "superseded", now()
    await s.flush()
    spec = {**a.spec, "recipes": []}
    new = await _insert(
        s, spec, status="active", source="system", user=SYSTEM, base_revision=a.revision,
        reviewed_by=SYSTEM, reviewed_at=now(), review_note=f"算子 {ref} 下线",
    )
    recipes = recipes_of(a.spec)
    await subscription.refresh(s, recipes)
    return {"feature_id": feature_id, "operator": operator, "version": version, "new_revision": new.revision,
            "recipes": sorted(recipes)}
