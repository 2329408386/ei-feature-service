#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""ei-feature-service 入口：uv run main.py

一个进程同时提供 HTTP 接口，并在后台运行实时计算、补算 / 重算和定时任务。
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from src import engine
from src.api import batches, bindings, features, gde, operators, schedules, values
from src.util import db, errors
from src.util.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时建表并启动后台任务，退出时停止后台任务并关闭连接池。"""
    db.configure()
    await db.init_schema()  # 建表语句均为 IF NOT EXISTS，可重复执行
    tasks = engine.start()
    yield
    await engine.stop(tasks)
    await db.dispose()


app = FastAPI(title="ei-feature-service", lifespan=lifespan)
errors.install(app)
for module in (features, operators, bindings, gde, schedules, batches, values):
    app.include_router(module.router, prefix="/api")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """健康检查。"""
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
