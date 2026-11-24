"""取数并发控制：全局上限、每腔室上限、补算上限，以及 GDE 熔断器。"""

import asyncio
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager

from src.util.config import (
    BATCH_CONCURRENCY,
    BREAKER_FAILURES,
    BREAKER_PAUSE_S,
    FETCH_CONCURRENCY,
    FETCH_PER_CHAMBER,
)
from src.util.gde import GdeUnavailable


class Breaker:
    """连续 failures 次失败后打开 pause_s 秒，期间直接拒绝；任何一次成功清零。"""

    def __init__(self, failures: int = BREAKER_FAILURES, pause_s: float = BREAKER_PAUSE_S):
        self.failures, self.pause_s = failures, pause_s
        self.count, self.open_until = 0, 0.0

    def check(self) -> None:
        if time.monotonic() < self.open_until:
            raise GdeUnavailable("熔断中：GDE 连续不可用，暂停取数")

    def success(self) -> None:
        self.count = 0

    def failure(self) -> None:
        if time.monotonic() < self.open_until:  # 熔断期间被拒绝的调用不再计数
            return
        self.count += 1
        if self.count >= self.failures:
            self.open_until, self.count = time.monotonic() + self.pause_s, 0


class Limits:
    def __init__(self):
        self.fetch = asyncio.Semaphore(FETCH_CONCURRENCY)
        self.batch = asyncio.Semaphore(BATCH_CONCURRENCY)
        self.chambers: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(FETCH_PER_CHAMBER))
        self.breaker = Breaker()

    @asynccontextmanager
    async def slot(self, chamber: str, backfill: bool = False):
        """取一个取数名额：补算 / 重算额外受 batch 信号量限制，保证实时计算优先。"""
        self.breaker.check()
        async with AsyncExitStack() as stack:
            if backfill:
                await stack.enter_async_context(self.batch)
            await stack.enter_async_context(self.fetch)
            await stack.enter_async_context(self.chambers[chamber])
            yield


limits = Limits()
