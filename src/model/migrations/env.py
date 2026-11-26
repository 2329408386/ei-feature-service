#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""Alembic 运行环境：数据库地址取自 config/config.json；迁移在 advisory lock 内执行，多实例同时启动只有一个在迁移。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text

from src.util.config import settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 默认用 config.json 里的库；测试可通过 Config.attributes["url"] 指定别的库
URL = config.attributes.get("url") or settings.database.url
LOCK_SQL = "SELECT pg_advisory_lock(hashtext('ei-feature-service:schema'))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('ei-feature-service:schema'))"
BASELINE = "0001"


def adopt_existing(conn) -> None:
    """接入迁移工具之前建的库：已有业务表但没有 alembic_version 时，记为第 1 版（只记版本，不执行建表）。"""
    has = lambda t: conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": t}).scalar()
    if has("feature") and not has("alembic_version"):
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": BASELINE})
        conn.commit()


def run_offline() -> None:
    """离线模式：只输出 SQL（alembic upgrade head --sql），不连库。"""
    context.configure(url=URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    """在线模式：psycopg 3 同步连接执行迁移，整个过程持有会话级 advisory lock。"""
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            conn.execute(text(LOCK_SQL))
            conn.commit()
            try:
                adopt_existing(conn)
                context.configure(connection=conn, transaction_per_migration=True)
                with context.begin_transaction():
                    context.run_migrations()
                conn.commit()
            finally:
                conn.execute(text(UNLOCK_SQL))
                conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
