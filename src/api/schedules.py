"""调度接口：/api/schedules"""

from fastapi import APIRouter

from src.api.schemas import ScheduleOut
from src.service import schedule as svc
from src.util.auth import User

router = APIRouter(prefix="/schedules", tags=["调度"])


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(tool: str | None = None, chamber: str | None = None):
    return await svc.list_schedules(tool, chamber)


@router.post("/{chamber}/pause", status_code=204)
async def pause(chamber: str, _: User):
    await svc.set_paused(chamber, True)


@router.post("/{chamber}/resume", status_code=204)
async def resume(chamber: str, _: User):
    await svc.set_paused(chamber, False)
