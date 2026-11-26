#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""配置：连接信息读自 config/config.json，其余参数是下面的代码常量。"""

import json
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "config.json"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class DatabaseConfig(BaseModel):
    url: str


class RedisConfig(BaseModel):
    url: str


class MinioConfig(BaseModel):
    # endpoint 以 file:// 开头时改用本地目录存算子文件（本地开发、测试用）
    endpoint: str
    access_key: str = ""
    secret_key: str = ""
    secure: bool = False
    bucket: str = "ei-feature-service"


class GdeConfig(BaseModel):
    fake: bool = False  # true：使用内存版 FakeGdeClient
    sdk: dict = {}  # 原样传给 GDE SDK 初始化


class Settings(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig
    redis: RedisConfig
    minio: MinioConfig
    gde: GdeConfig = GdeConfig()


def load_settings(path: Path = CONFIG_FILE) -> Settings:
    """读取并校验配置；缺字段或类型错误时直接抛异常，让启动失败。"""
    return Settings.model_validate(json.loads(path.read_text(encoding="utf-8")))


settings = load_settings()

# ---------------------------------------------------------------- 代码常量
RUN_DONE_STREAM = "gde:run:done"  # GDE 执行结束通知的 Redis Stream
CONSUMER_GROUP = "ei-feature-service"

FETCH_CONCURRENCY = 8  # 每个实例同时取数的上限
FETCH_PER_CHAMBER = 2  # 每个腔室同时取数的上限
BATCH_CONCURRENCY = 3  # 补算 / 重算最多占用的取数并发
TRIAL_CONCURRENCY = 2  # 同时进行的试跑数

FETCH_TIMEOUT_S = 30
COMPUTE_TIMEOUT_S = 10
SANDBOX_PROCESSES = 8

CLAIM_IDLE_S = 300  # 未确认的消息闲置多久后被认领重试
MAX_DELIVERIES = 3  # 一条消息最多投递次数

BREAKER_FAILURES = 5  # 连续多少次 GDE 不可用后熔断
BREAKER_PAUSE_S = 60  # 熔断持续秒数

RECIPE_CHAMBERS_TTL_S = 60  # 特征页 / 调度页缓存 GDE recipe 反查结果的秒数
BATCH_MAX_DAYS = 31  # 补算 / 重算单次最大天数
TZ = "Asia/Shanghai"  # 日期参数、实时批次整点所用时区
PACKAGE_CACHE_DIR = Path("/tmp/ei-feature/pkgs")
