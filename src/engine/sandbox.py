#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""沙箱：在独立子进程里执行特征包的 fetch(run_id) 和 compute(data)。

- 每个阶段用 SIGALRM 计时，超时记 FETCH_TIMEOUT / COMPUTE_ERROR；
- 子进程卡死时由 pebble 的总超时强制杀掉；
- 异常一律转成原因码，只有 GDE 不可用会抛给调用方（由调用方决定重试）。
"""

import asyncio
import importlib.util
import math
import multiprocessing
import signal
import sys
import time
import traceback
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache

from pebble import ProcessExpired, ProcessPool

from src.util.config import COMPUTE_TIMEOUT_S, FETCH_TIMEOUT_S, SANDBOX_PROCESSES, settings
from src.util.gde import GdeNotFound, GdeUnavailable, make_gde
from src.util.storage import CODE_FILE, local_package

DETAIL_MAX = 1024  # reason_detail 最长 1 KB


@dataclass
class RunResult:
    value: float | None = None
    reason: str | None = None
    reason_detail: str | None = None
    data_summary: dict = field(default_factory=dict)
    fetch_ms: int = 0
    compute_ms: int = 0


# ====================================================================== 子进程内
class _Timeout(Exception):
    pass


@contextmanager
def _alarm(seconds: float):
    def _raise(*_):
        raise _Timeout()

    old = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@cache
def _child_gde(gde_json: str):
    from src.util.config import GdeConfig

    return make_gde(GdeConfig.model_validate_json(gde_json))


_modules: dict[str, object] = {}


def _load(pkg_dir: str, key: str):
    """按 (id, version, hash) 加载一次 compute.py 并缓存。"""
    if key not in _modules:
        import src.eifeature

        sys.modules.setdefault("eifeature", src.eifeature)  # 特征包写的是 from eifeature import ...
        spec = importlib.util.spec_from_file_location(key, f"{pkg_dir}/{CODE_FILE}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _modules[key] = module
    return _modules[key]


def _detail(e: BaseException) -> str:
    return "".join(traceback.format_exception_only(e)).strip()[:DETAIL_MAX]


def _summary(data) -> dict:
    """data 中形如 {"t": [...], "v": [...]}（或 Series）的顶层键：点数和时间范围。"""
    out = {}
    for k, x in (data.items() if isinstance(data, dict) else []):
        t = x.get("t") if isinstance(x, dict) else getattr(x, "t", None)
        if isinstance(t, list):
            out[k] = {"points": len(t), "t_min": min(t, default=None), "t_max": max(t, default=None)}
    return out


def _execute(pkg_dir: str, key: str, run_id: str, gde_json: str, fetch_s: float, compute_s: float) -> dict:
    """子进程入口：返回 RunResult 的字段；GDE 不可用时返回 {"unavailable": 说明}。"""
    from src.eifeature import REASON, Skip
    from src.eifeature import gde as eif_gde

    r = RunResult()
    module = _load(pkg_dir, key)

    def skip(e: Skip, fallback: str) -> None:  # 原因码不合规时当作错误处理
        r.reason = e.reason if REASON.match(str(e.reason)) else fallback
        r.reason_detail = None if r.reason == e.reason else f"非法原因码：{e.reason!r}"

    # ---- fetch：可以调用 gde 取数
    t0 = time.perf_counter()
    eif_gde.bind(_child_gde(gde_json), fetch_s)
    try:
        with _alarm(fetch_s):
            data = module.fetch(run_id)
    except Skip as e:
        skip(e, "FETCH_ERROR")
    except _Timeout:
        r.reason = "FETCH_TIMEOUT"
    except GdeNotFound as e:
        r.reason, r.reason_detail = "MISSING_DATA", _detail(e)
    except GdeUnavailable as e:
        return {"unavailable": str(e)}
    except Exception as e:
        r.reason, r.reason_detail = "FETCH_ERROR", _detail(e)
    finally:
        eif_gde.bind(None)
        r.fetch_ms = int((time.perf_counter() - t0) * 1000)
    if r.reason:
        return r.__dict__

    # ---- compute：纯计算，调用 gde 会报错
    r.data_summary = _summary(data)
    t0 = time.perf_counter()
    try:
        with _alarm(compute_s):
            value = module.compute(data)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            r.reason, r.reason_detail = "COMPUTE_ERROR", f"compute 返回了非有限数值：{value!r}"[:DETAIL_MAX]
        else:
            r.value = float(value)
    except Skip as e:
        skip(e, "COMPUTE_ERROR")
    except _Timeout:
        r.reason, r.reason_detail = "COMPUTE_ERROR", f"compute 超过 {compute_s} 秒"
    except Exception as e:
        r.reason, r.reason_detail = "COMPUTE_ERROR", _detail(e)
    r.compute_ms = int((time.perf_counter() - t0) * 1000)
    return r.__dict__


# ====================================================================== 主进程
class Sandbox:
    def __init__(self, processes: int = SANDBOX_PROCESSES):
        # spawn：子进程不继承主进程的事件循环、连接池和线程
        self.pool = ProcessPool(max_workers=processes, max_tasks=500, context=multiprocessing.get_context("spawn"))
        self.gde_json = settings.gde.model_dump_json()

    async def run(self, feature_id: str, version: str, package_hash: str, run_id: str) -> RunResult:
        """执行一次 fetch + compute。GDE 不可用时抛 GdeUnavailable，其他问题都体现在 reason 上。"""
        pkg_dir = await asyncio.to_thread(local_package, feature_id, version, package_hash)
        key = f"eif_pkg_{feature_id}_{version.replace('.', '_')}_{package_hash[:12]}"
        future = self.pool.schedule(
            _execute,
            args=(str(pkg_dir), key, run_id, self.gde_json, FETCH_TIMEOUT_S, COMPUTE_TIMEOUT_S),
            timeout=FETCH_TIMEOUT_S + COMPUTE_TIMEOUT_S + 10,
        )
        try:
            out = await asyncio.wrap_future(future)
        except FutureTimeout:
            return RunResult(reason="COMPUTE_ERROR", reason_detail="沙箱总超时，子进程已被终止")
        except ProcessExpired as e:
            return RunResult(reason="COMPUTE_ERROR", reason_detail=f"沙箱子进程异常退出：{e}"[:DETAIL_MAX])
        if "unavailable" in out:
            raise GdeUnavailable(out["unavailable"])
        return RunResult(**out)

    def close(self) -> None:
        """停止进程池并等待子进程退出。"""
        self.pool.stop()
        self.pool.join(timeout=5)


_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    """进程内单例，首次使用时创建进程池。"""
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox()
    return _sandbox


def close_sandbox() -> None:
    """关闭单例进程池（服务退出时调用）。"""
    global _sandbox
    if _sandbox is not None:
        _sandbox.close()
        _sandbox = None
