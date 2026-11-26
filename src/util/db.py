#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""数据库：异步引擎、会话、迁移、按键加锁。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.util.config import settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def configure(url: str | None = None) -> None:
    """创建（或按新地址重建）引擎。不调用时首次使用自动按配置创建。"""
    global _engine, _sessions
    _engine = create_async_engine(url or settings.database.url, pool_pre_ping=True)
    _sessions = async_sessionmaker(_engine, expire_on_commit=False)


def engine() -> AsyncEngine:
    """当前引擎；尚未创建时按配置创建。"""
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


def alembic_config() -> Config:
    """Alembic 配置（alembic.ini），不让它改动服务自己的日志设置。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["configure_logger"] = False
    return cfg


async def migrate(revision: str = "head") -> None:
    """把库升级到指定版本（默认最新）。迁移在 src/model/migrations/versions/，env.py 内加锁，多实例同时启动不冲突。"""
    await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def lock(s: AsyncSession, key: str) -> None:
    """事务级 advisory lock，同一 key 的事务串行执行（如同一腔室生成修订号）。"""
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


async def dispose() -> None:
    """关闭连接池。"""
    if _engine is not None:
        await _engine.dispose()
