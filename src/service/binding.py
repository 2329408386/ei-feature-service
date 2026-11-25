#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""腔室绑定：校验、提交、审核、撤回、查询、导入导出，以及特征下线时的自动修改。

修订状态：(新建) -> pending --approve(他人)--> active（原 active -> superseded）
                   pending --reject(他人)--> rejected / --withdraw(本人)--> withdrawn
source=system 的修订（特征下线时生成）直接以 active 插入。
"""

import asyncio
import copy

from pydantic import ValidationError
from sqlalchemy import func, select, tuple_

from src.api.schemas import EXPORT_FORMAT, BindingSpec
from src.model.tables import BindingItem, ChamberBinding, Feature, GdeOutbox
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
from src.util.gde import GdeError, get_gde
from src.util.timeutil import now


# ---------------------------------------------------------------- 小工具
def recipes_of(spec: dict) -> list[str]:
    """spec 中全部规则的配方，去重排序（即订阅范围）。"""
    return sorted({r for rule in spec["rules"] for r in rule["recipes"]})


def features_of(spec: dict) -> set[str]:
    """spec 中全部规则的特征，形如 id@version。"""
    return {f"{fid}@{ver}" for rule in spec["rules"] for fid, ver in rule["features"].items()}


def _normalize(spec: BindingSpec, recipe_order: list[str]) -> dict:
    """规范化：配方按 GDE 返回的顺序，特征按 ID 排序；规则保持原顺序。"""
    order = {r: i for i, r in enumerate(recipe_order)}
    return {
        "tool": spec.tool,
        "chamber": spec.chamber,
        "rules": [
            {
                "recipes": sorted(set(r.recipes), key=lambda x: order.get(x, len(order))),
                "features": dict(sorted(r.features.items())),
            }
            for r in spec.rules
        ],
    }


async def _gde(fn, *args):
    """在线程里调用 GDE；失败转成 502。"""
    try:
        return await asyncio.to_thread(fn, *args)
    except GdeError as e:
        raise gde_unavailable(str(e)) from None


async def _revision(s, chamber: str, status: str) -> ChamberBinding | None:
    stmt = select(ChamberBinding).where(ChamberBinding.chamber == chamber, ChamberBinding.status == status)
    return (await s.execute(stmt.with_for_update())).scalar_one_or_none()


async def active(s, chamber: str) -> ChamberBinding | None:
    """腔室的生效修订（加行锁），没有则返回 None。"""
    return await _revision(s, chamber, "active")


async def pending(s, chamber: str) -> ChamberBinding | None:
    """腔室的待审核修订（加行锁），没有则返回 None。"""
    return await _revision(s, chamber, "pending")


# ---------------------------------------------------------------- 校验
async def validate(s, spec: BindingSpec) -> tuple[dict, list[dict]]:
    """绑定校验（提交、导入共用）：返回 (规范化后的 spec, 全部错误)。"""
    gde, errors = get_gde(), []
    tool = await _gde(gde.chamber_tool, spec.chamber)
    if tool is None:
        return {}, [{"path": "chamber", "message": f"腔室 {spec.chamber} 不存在"}]
    if tool != spec.tool:
        errors.append({"path": "tool", "message": f"腔室 {spec.chamber} 属于机台 {tool}，不是 {spec.tool}"})
    known = await _gde(gde.chamber_recipes, spec.chamber)
    errors += _check_rules(spec, known)
    errors += await _check_features(s, spec)
    return _normalize(spec, known), errors


def _check_rules(spec: BindingSpec, known: list[str]) -> list[dict]:
    """规则结构：至少一条规则，每条规则有配方和特征，配方属于该腔室且不跨规则重复。"""
    errors = []
    if not spec.rules:
        errors.append({"path": "rules", "message": "至少需要一条规则"})
    seen: dict[str, int] = {}
    for i, rule in enumerate(spec.rules):
        if not rule.recipes:
            errors.append({"path": f"rules[{i}].recipes", "message": "至少选择一个配方"})
        if not rule.features:
            errors.append({"path": f"rules[{i}].features", "message": "至少选择一个特征"})
        for j, recipe in enumerate(rule.recipes):
            path = f"rules[{i}].recipes[{j}]"
            if recipe not in known:
                errors.append({"path": path, "message": f"配方 {recipe} 不在腔室 {spec.chamber}"})
            elif seen.setdefault(recipe, i) != i:
                errors.append({"path": path, "message": f"配方 {recipe} 已在规则 {seen[recipe] + 1} 中"})
    return errors


async def _check_features(s, spec: BindingSpec) -> list[dict]:
    """引用的特征必须存在且已发布。"""
    wanted = {
        (fid, ver)
        for rule in spec.rules
        for fid, ver in rule.features.items()
    }
    if not wanted:
        return []
    stmt = select(Feature.feature_id, Feature.version).where(
        tuple_(Feature.feature_id, Feature.version).in_(wanted), Feature.status == "published"
    )
    published = set((await s.execute(stmt)).tuples().all())
    errors = []
    for i, rule in enumerate(spec.rules):
        for fid, ver in rule.features.items():
            if (fid, ver) not in published:
                errors.append({"path": f"rules[{i}].features.{fid}", "message": f"{fid}@{ver} 不存在或未发布"})
    return errors


async def _check_state(s, chamber: str, spec: dict) -> None:
    """已有待审核 → PENDING_EXISTS；与生效修订一致 → NO_CHANGE。"""
    p = await pending(s, chamber)
    if p:
        raise conflict("PENDING_EXISTS", f"腔室 {chamber} 已有待审核修订 {p.revision}")
    a = await active(s, chamber)
    if a and a.spec == spec:
        raise conflict("NO_CHANGE", f"与生效修订 {a.revision} 相同")


async def _insert(
    s, spec: dict, *, status: str, source: str, user: str, base_revision: int | None, **extra
) -> ChamberBinding:
    """新增修订（修订号 = 最大值 + 1），并在同一事务里展开写 binding_item。调用方须已持有腔室锁。"""
    chamber = spec["chamber"]
    last = await s.scalar(select(func.max(ChamberBinding.revision)).where(ChamberBinding.chamber == chamber))
    row = ChamberBinding(
        chamber=chamber, revision=(last or 0) + 1, tool=spec["tool"], spec=spec, status=status,
        source=source, base_revision=base_revision, created_by=user, **extra,
    )
    s.add(row)
    await s.flush()
    for i, rule in enumerate(spec["rules"], start=1):
        s.add_all(
            BindingItem(chamber=chamber, revision=row.revision, rule_no=i, recipe=recipe, feature_id=fid, version=ver)
            for recipe in rule["recipes"]
            for fid, ver in rule["features"].items()
        )
    await s.flush()
    return row


# ---------------------------------------------------------------- 提交、审核、撤回
async def submit(chamber: str, body: BindingSpec, base_revision: int | None, user: str, source: str) -> ChamberBinding:
    """校验并生成待审核修订。同一腔室同时只能有一个待审核修订。"""
    if body.chamber != chamber:
        raise invalid("BINDING_INVALID", "绑定校验失败", [{"path": "chamber", "message": "与路径中的腔室不一致"}])
    async with db.session() as s:
        await db.lock(s, f"binding:{chamber}")
        spec, errors = await validate(s, body)
        if errors:
            raise invalid("BINDING_INVALID", "绑定校验失败", errors)
        await _check_state(s, chamber, spec)
        return await _insert(s, spec, status="pending", source=source, user=user, base_revision=base_revision)


async def _get_revision(s, chamber: str, revision: int) -> ChamberBinding:
    stmt = select(ChamberBinding).where(ChamberBinding.chamber == chamber, ChamberBinding.revision == revision)
    row = (await s.execute(stmt.with_for_update())).scalar_one_or_none()
    if row is None:
        raise not_found(f"腔室 {chamber} 的修订 {revision}")
    return row


async def review(chamber: str, revision: int, decision: str, comment: str | None, user: str) -> ChamberBinding:
    """审核。通过时先调 SDK 注册 / 更新订阅，成功后再改库；SDK 失败则什么都不改。"""
    comment = (comment or "").strip() or None
    gde, new_sub = get_gde(), None
    try:
        async with db.session() as s:
            await db.lock(s, f"binding:{chamber}")
            p = await _get_revision(s, chamber, revision)
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

            a = await active(s, chamber)
            sub_id = a.gde_sub_id if a else None
            if not sub_id:
                sub_id = new_sub = await _gde(gde.subscribe, p.tool, chamber, recipes_of(p.spec))
            elif recipes_of(p.spec) != recipes_of(a.spec):
                await _gde(gde.update_subscription, sub_id, recipes_of(p.spec))
            if a:
                a.status, a.superseded_at = "superseded", now()
                await s.flush()  # 先让旧修订退出 active，避免违反“每腔室一个 active”的唯一索引
            p.status, p.gde_sub_id = "active", sub_id
            return p
    except Exception:
        if new_sub:  # 改库失败，撤销刚注册的订阅
            await asyncio.to_thread(gde.cancel_subscription, new_sub)
        raise


async def withdraw(chamber: str, revision: int, user: str) -> ChamberBinding:
    """提交人撤回自己的待审核修订。"""
    async with db.session() as s:
        p = await _get_revision(s, chamber, revision)
        if p.status != "pending":
            raise invalid_state("只有待审核的修订可以撤回", p.status)
        if p.created_by != user:
            raise forbidden("只有提交人可以撤回")
        p.status = "withdrawn"
    return p


# ---------------------------------------------------------------- 查询与导出
async def list_bindings(tool=None, chamber=None, recipe=None, feature=None, status=None) -> list[dict]:
    """每个腔室一项：{tool, chamber, active, pending}。recipe / feature 按生效修订的 binding_item 过滤。

    未绑定的腔室后端不知道，由前端用 GDE 元数据补齐。
    """
    cond = [ChamberBinding.status.in_(["active", "pending"])]
    if tool:
        cond.append(ChamberBinding.tool == tool)
    if chamber:
        cond.append(ChamberBinding.chamber == chamber)
    async with db.session() as s:
        stmt = select(ChamberBinding).where(*cond).order_by(ChamberBinding.tool, ChamberBinding.chamber)
        rows = (await s.execute(stmt)).scalars().all()
        matched = None
        if recipe or feature:
            q = select(BindingItem.chamber).join(
                ChamberBinding,
                (ChamberBinding.chamber == BindingItem.chamber) & (ChamberBinding.revision == BindingItem.revision),
            ).where(ChamberBinding.status == "active")
            if recipe:
                q = q.where(BindingItem.recipe == recipe)
            if feature:
                fid, _, ver = feature.partition("@")
                q = q.where(BindingItem.feature_id == fid, *([BindingItem.version == ver] if ver else []))
            matched = set((await s.execute(q)).scalars().all())

    out: dict[str, dict] = {}
    for r in rows:
        item = out.setdefault(r.chamber, {"tool": r.tool, "chamber": r.chamber, "active": None, "pending": None})
        item[r.status] = r
    return [
        x for x in out.values()
        if (status is None or x[status] is not None) and (matched is None or x["chamber"] in matched)
    ]


async def get_revisions(chamber: str, revision: int | None = None):
    """不带 revision 返回腔室全部修订（新的在前）；带 revision 返回单个修订。"""
    async with db.session() as s:
        if revision is not None:
            return await _get_revision(s, chamber, revision)
        stmt = select(ChamberBinding).where(ChamberBinding.chamber == chamber).order_by(ChamberBinding.revision.desc())
        return (await s.execute(stmt)).scalars().all()


async def export(**filters) -> dict:
    """导出生效修订，格式可以直接再导入。"""
    items = await list_bindings(**filters)
    actives = [x["active"] for x in items]
    return {
        "format": EXPORT_FORMAT,
        "exported_at": now().isoformat(),
        "bindings": [
            {"tool": a.tool, "chamber": a.chamber, "revision": a.revision, "rules": a.spec["rules"]}
            for a in actives
            if a and a.spec["rules"]
        ],
    }


# ---------------------------------------------------------------- 导入
async def import_bindings(bindings: list[dict], dry_run: bool, user: str) -> list[dict]:
    """逐个腔室校验：new / skip / conflict / error。dry_run=False 时为所有 new 生成待审核修订（同一事务）。"""
    results, seen = [], set()
    async with db.session() as s:
        for raw in bindings:
            tool, chamber = raw.get("tool"), raw.get("chamber")
            res = {"tool": tool, "chamber": chamber, "action": "error", "message": ""}
            results.append(res)
            try:
                spec = BindingSpec.model_validate(raw)
            except ValidationError as e:
                res["message"], res["errors"] = "格式错误", error_list(e)
                continue
            if chamber in seen:
                res["message"] = "文件中重复出现"
                continue
            seen.add(chamber)

            await db.lock(s, f"binding:{chamber}")
            normalized, errors = await validate(s, spec)
            if errors:
                res["message"], res["errors"] = "校验失败", errors
                continue
            try:
                await _check_state(s, chamber, normalized)
            except AppError as e:
                res["action"] = "conflict" if e.code == "PENDING_EXISTS" else "skip"
                res["message"] = e.message
                continue

            a = await active(s, chamber)
            res["action"], res["message"] = "new", "生成待审核修订"
            if raw.get("revision") and a and raw["revision"] != a.revision:
                res["message"] += f"（文件基于修订 {raw['revision']}，当前生效修订 {a.revision}）"
            if not dry_run:
                row = await _insert(
                    s, normalized, status="pending", source="import", user=user,
                    base_revision=a.revision if a else None,
                )
                res["revision"] = row.revision
    return results


# ---------------------------------------------------------------- 特征下线
async def remove_feature(s, feature_id: str, version: str) -> list[dict]:
    """在调用方的事务里：从所有生效修订中移除该特征（生成 system 修订直接生效），撤回含该特征的待审核修订。

    订阅变更写入 gde_outbox，提交后再执行。
    """
    joined = select(ChamberBinding).join(
        BindingItem, (BindingItem.chamber == ChamberBinding.chamber) & (BindingItem.revision == ChamberBinding.revision)
    ).where(BindingItem.feature_id == feature_id, BindingItem.version == version).distinct()

    # 待审核修订含该特征：撤回
    for p in (await s.execute(joined.where(ChamberBinding.status == "pending"))).scalars():
        p.status, p.review_note = "withdrawn", f"特征 {feature_id}@{version} 下线，自动撤回"

    affected = []
    for a in (await s.execute(joined.where(ChamberBinding.status == "active"))).scalars().all():
        await db.lock(s, f"binding:{a.chamber}")
        spec = copy.deepcopy(a.spec)
        for rule in spec["rules"]:
            if rule["features"].get(feature_id) == version:
                del rule["features"][feature_id]
        spec["rules"] = [r for r in spec["rules"] if r["features"]]

        p = await pending(s, a.chamber)
        if p:  # 该腔室的其他待审核修订基于旧内容，也一并撤回
            p.status, p.review_note = "withdrawn", f"特征 {feature_id}@{version} 下线，自动撤回，请基于新修订重新提交"
        a.status, a.superseded_at = "superseded", now()
        await s.flush()

        keep = bool(spec["rules"]) and a.gde_sub_id is not None
        new = await _insert(
            s, spec, status="active", source="system", user=SYSTEM, base_revision=a.revision,
            reviewed_by=SYSTEM, reviewed_at=now(), review_note=f"特征 {feature_id}@{version} 下线",
            gde_sub_id=a.gde_sub_id if keep else None,
        )
        if a.gde_sub_id and not keep:
            s.add(GdeOutbox(action="cancel_subscription", payload={"sub_id": a.gde_sub_id}))
        elif keep and recipes_of(spec) != recipes_of(a.spec):
            payload = {"sub_id": a.gde_sub_id, "recipes": recipes_of(spec)}
            s.add(GdeOutbox(action="update_subscription", payload=payload))
        subscription = "kept" if keep else "cancelled"
        affected.append({"chamber": a.chamber, "new_revision": new.revision, "subscription": subscription})
    return affected
