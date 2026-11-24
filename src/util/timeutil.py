"""时间工具：所有日期参数、实时批次整点都按 config.TZ 解释。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.util.config import TZ

ZONE = ZoneInfo(TZ)


def now() -> datetime:
    return datetime.now(ZONE)


def today() -> date:
    return now().date()


def day_range(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 当天 0 点, end 次日 0 点)，即 end 当天包含在内。"""
    return datetime.combine(start, time(), ZONE), datetime.combine(end + timedelta(days=1), time(), ZONE)


def hour_floor(t: datetime) -> datetime:
    """t 所在整点（本地时区）。"""
    return t.astimezone(ZONE).replace(minute=0, second=0, microsecond=0)
