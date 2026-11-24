"""数据库：异步引擎、会话、建表、按键加锁。"""

from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.util.config import settings

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "model" / "schema.sql"

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def configure(url: str | None = None) -> None:
    """创建（或按新地址重建）引擎。不调用时首次使用自动按配置创建。"""
    global _engine, _sessions
    _engine = create_async_engine(url or settings.database.url, pool_pre_ping=True)
    _sessions = async_sessionmaker(_engine, expire_on_commit=False)


def engine() -> AsyncEngine:
    if _engine is None:
        configure()
    return _engine


@asynccontextmanager
async def session():
    """一个事务：正常结束提交，抛异常回滚。"""
    if _sessions is None:
        configure()
    async with _sessions() as s, s.begin():
        yield s


async def init_schema() -> None:
    """执行 schema.sql；语句全部 IF NOT EXISTS，可以重复执行。加锁避免多实例同时启动时互相冲突。"""
    async with engine().begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('ei-feature-service:schema'))"))
        await conn.exec_driver_sql(SCHEMA_FILE.read_text(encoding="utf-8"))


async def lock(s: AsyncSession, key: str) -> None:
    """事务级 advisory lock，同一 key 的事务串行执行（如同一腔室生成修订号）。"""
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()
