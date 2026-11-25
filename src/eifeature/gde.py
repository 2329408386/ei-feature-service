#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子取数接口：只能在 fetch 中调用。

沙箱在执行前通过 bind() 注入 GDE 客户端；进入 compute 阶段后再调用会抛 RuntimeError，
保证 compute 不做 IO，同样的 data 总得到同样的结果。
"""

from src.eifeature.series import Series, Step
from src.util.gde import GdeClient, GdeNotFound, Run

_client: GdeClient | None = None
_timeout_s: float = 30.0
_runs: dict[str, Run] = {}


def bind(client: GdeClient | None, timeout_s: float = 30.0) -> None:
    """由沙箱调用：传 client 进入 fetch 阶段，传 None 进入 compute 阶段。"""
    global _client, _timeout_s
    _client, _timeout_s = client, timeout_s
    if client is None:
        _runs.clear()


def _require() -> GdeClient:
    if _client is None:
        raise RuntimeError("gde 只能在 fetch 中调用，compute 不能取数")
    return _client


def run_info(run_id: str) -> Run:
    """执行的机台、腔室、配方、晶圆和步骤。run_id 不存在时抛 GdeNotFound（记为 MISSING_DATA）。"""
    if run_id not in _runs:
        _runs[run_id] = _require().run_info(run_id)
    return _runs[run_id]


def trace(run_id: str, signal: str, unit: str | None = None) -> Series:
    """取一个信号的时间序列。信号不存在抛 Skip("MISSING_SIGNAL")。

    TODO: 单位换算由 GDE SDK 完成，SDK 不支持的单位应抛 Skip("BAD_UNIT")，待 SDK 确认后补上。
    """
    from src.eifeature import Skip

    run = run_info(run_id)
    try:
        t, v = _require().trace(run_id, signal, unit, _timeout_s)
    except GdeNotFound:
        raise Skip("MISSING_SIGNAL") from None
    steps = tuple(Step(s.id, s.start_s, s.end_s) for s in run.steps)
    return Series(list(t), list(v), steps)
