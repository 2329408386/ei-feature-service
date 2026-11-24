"""调度：各腔室的订阅状态与处理进度，暂停 / 恢复订阅。订阅状态以 GDE 为准。"""

import asyncio

from sqlalchemy import func, select

from src.model.tables import BindingItem, ChamberBinding, FeatureBatch, FeatureValue
from src.service.binding import _gde, active
from src.util import db
from src.util.errors import invalid_state, not_found
from src.util.gde import GdeError, get_gde


async def _status(sub_id: str | None) -> str:
    if not sub_id:
        return "cancelled"
    try:
        return await asyncio.to_thread(get_gde().subscription_status, sub_id)
    except GdeError:
        return "unknown"  # 查不到状态不影响列表


async def list_schedules(tool: str | None = None, chamber: str | None = None) -> list[dict]:
    cond = [ChamberBinding.status == "active"]
    if tool:
        cond.append(ChamberBinding.tool == tool)
    if chamber:
        cond.append(ChamberBinding.chamber == chamber)
    async with db.session() as s:
        rows = (await s.execute(select(ChamberBinding).where(*cond).order_by(ChamberBinding.chamber))).scalars().all()
        counts = dict(
            (await s.execute(
                select(BindingItem.chamber, func.count(func.distinct(BindingItem.feature_id + "@" + BindingItem.version)))
                .join(ChamberBinding, (ChamberBinding.chamber == BindingItem.chamber) & (ChamberBinding.revision == BindingItem.revision))
                .where(*cond).group_by(BindingItem.chamber)
            )).tuples().all()
        )
        # 处理进度：实时批次里最新的执行结束时间
        progress = dict(
            (await s.execute(
                select(FeatureValue.chamber, func.max(FeatureValue.run_end_at))
                .join(FeatureBatch, FeatureBatch.batch_id == FeatureValue.batch_id)
                .where(FeatureBatch.mode == "live").group_by(FeatureValue.chamber)
            )).tuples().all()
        )
    statuses = await asyncio.gather(*(_status(r.gde_sub_id) for r in rows))
    return [
        {
            "tool": r.tool, "chamber": r.chamber, "gde_sub_id": r.gde_sub_id, "binding_revision": r.revision,
            "feature_count": counts.get(r.chamber, 0), "status": st, "progress": progress.get(r.chamber),
        }
        for r, st in zip(rows, statuses)
    ]


async def set_paused(chamber: str, paused: bool) -> None:
    async with db.session() as s:
        a = await active(s, chamber)
    if a is None:
        raise not_found(f"腔室 {chamber} 的生效绑定")
    if not a.gde_sub_id:
        raise invalid_state("该腔室没有 GDE 订阅", "cancelled")
    gde = get_gde()
    await _gde(gde.pause_subscription if paused else gde.resume_subscription, a.gde_sub_id)
