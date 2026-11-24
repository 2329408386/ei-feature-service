"""GDE 适配层：服务内部只依赖 GdeClient 协议。

- SdkGdeClient：对接真实 GDE SDK（函数名待数据底座确认，目前是 TODO）。
- FakeGdeClient：内存实现，数据按 run_id 确定性生成，供本地开发和测试使用。
所有方法都是同步的；在 async 代码里用 asyncio.to_thread 调用。
"""

import itertools
import random
import re
import threading
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from typing import Literal, Protocol

from src.util.config import GdeConfig, settings
from src.util.timeutil import ZONE


class GdeError(Exception):
    pass


class GdeUnavailable(GdeError):
    """网络错误、5xx、超时：可以重试。"""


class GdeNotFound(GdeError):
    """run_id、信号、腔室不存在：重试没有意义。"""


@dataclass(frozen=True)
class Step:
    id: int
    start_s: float  # 相对执行开始的秒数
    end_s: float


@dataclass(frozen=True)
class Run:
    run_id: str
    tool: str
    chamber: str
    recipe: str
    lot: str | None
    wafer: str | None
    start_at: datetime
    end_at: datetime
    steps: list[Step] = field(default_factory=list)


SubStatus = Literal["running", "paused", "cancelled"]


class GdeClient(Protocol):
    # 元数据：只用于后端校验，前端直接调 GDE 元数据服务
    def chamber_tool(self, chamber: str) -> str | None: ...
    def chamber_recipes(self, chamber: str) -> list[str]: ...

    # 订阅
    def subscribe(self, tool: str, chamber: str, recipes: list[str]) -> str: ...
    def update_subscription(self, sub_id: str, recipes: list[str]) -> None: ...
    def pause_subscription(self, sub_id: str) -> None: ...
    def resume_subscription(self, sub_id: str) -> None: ...
    def cancel_subscription(self, sub_id: str) -> None: ...
    def subscription_status(self, sub_id: str) -> SubStatus: ...

    # 执行与数据
    def list_runs(self, chamber: str, recipes: list[str], start: datetime, end: datetime) -> list[Run]: ...
    def run_info(self, run_id: str) -> Run: ...
    def trace(
        self, run_id: str, signal: str, unit: str | None, timeout_s: float
    ) -> tuple[list[float], list[float]]: ...


# ====================================================================== 真实 SDK
class SdkGdeClient:
    """对接 GDE SDK。

    TODO: 数据底座确认 SDK 包名和函数后实现。约定：
      - 网络错误 / 5xx / 超时 统一转成 GdeUnavailable；
      - 资源不存在转成 GdeNotFound；
      - trace 返回 (t, v)，t 为相对执行开始的秒数且升序。
    """

    def __init__(self, sdk_config: dict):
        self.sdk_config = sdk_config  # TODO: self.sdk = gde_sdk.Client(**sdk_config)

    def __getattr__(self, name: str):
        raise NotImplementedError(f"TODO: GDE SDK 尚未接入，无法调用 {name}；本地开发请在 config.json 设置 gde.fake=true")


# ====================================================================== 内存实现
FAKE_TOOLS = {
    "ETCH-01": {"CH-A01": ["RCP-0432", "RCP-0433", "RCP-0500"], "CH-A02": ["RCP-0432", "RCP-0433"]},
    "ETCH-02": {"CH-B02": ["RCP-1107", "RCP-1108"]},
}
FAKE_STEPS = [Step(210, 0, 30), Step(212, 30, 60), Step(220, 60, 75)]
_RUN_ID = re.compile(r"^RUN-(CH-[A-Z0-9]+)-(\d{12})$")  # list_runs 生成的 run_id：RUN-{腔室}-{yyyymmddHHMM}


def _seed(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode())  # 跨进程稳定（不用内置 hash）


class FakeGdeClient:
    """确定性的假 GDE：

    - run_id 为 RUN-{腔室}-{yyyymmddHHMM} 时按其解析；其他 run_id（如 RUN-0338）按哈希分配腔室和时间。
    - 以 RUN-MISSING 开头的 run_id 不存在；以 MISSING 开头的信号不存在。
    - 约 1/10 的执行数据稀疏（点数不足），用来产生 LOW_COVERAGE 之类的空值。
    - fail_next = n 时，接下来 n 次数据调用抛 GdeUnavailable（测试重试用）。
    """

    def __init__(self):
        self.chambers = {c: (t, rs) for t, cs in FAKE_TOOLS.items() for c, rs in cs.items()}
        self.subs: dict[str, dict] = {}
        self.fail_next = 0
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _maybe_fail(self) -> None:
        with self._lock:
            if self.fail_next > 0:
                self.fail_next -= 1
                raise GdeUnavailable("fake: 模拟 GDE 不可用")

    # ---- 元数据
    def chamber_tool(self, chamber):
        return self.chambers[chamber][0] if chamber in self.chambers else None

    def chamber_recipes(self, chamber):
        if chamber not in self.chambers:
            raise GdeNotFound(f"腔室 {chamber} 不存在")
        return list(self.chambers[chamber][1])

    # ---- 订阅
    def subscribe(self, tool, chamber, recipes):
        sub_id = f"gde-sub-{next(self._ids):04d}"
        self.subs[sub_id] = {"tool": tool, "chamber": chamber, "recipes": list(recipes), "status": "running"}
        return sub_id

    def _sub(self, sub_id) -> dict:
        if sub_id not in self.subs:
            raise GdeNotFound(f"订阅 {sub_id} 不存在")
        return self.subs[sub_id]

    def update_subscription(self, sub_id, recipes):
        self._sub(sub_id)["recipes"] = list(recipes)

    def pause_subscription(self, sub_id):
        self._sub(sub_id)["status"] = "paused"

    def resume_subscription(self, sub_id):
        self._sub(sub_id)["status"] = "running"

    def cancel_subscription(self, sub_id):
        self._sub(sub_id)["status"] = "cancelled"

    def subscription_status(self, sub_id):
        return self._sub(sub_id)["status"]

    # ---- 执行与数据
    def _make_run(self, run_id: str, chamber: str, end_at: datetime) -> Run:
        tool, recipes = self.chambers[chamber]
        n = _seed(run_id)
        return Run(
            run_id=run_id, tool=tool, chamber=chamber, recipe=recipes[n % len(recipes)],
            lot=f"L{n % 900 + 100}", wafer=f"W{n % 10000:04d}",
            start_at=end_at - timedelta(seconds=FAKE_STEPS[-1].end_s), end_at=end_at, steps=list(FAKE_STEPS),
        )

    def list_runs(self, chamber, recipes, start, end):
        self._maybe_fail()
        if chamber not in self.chambers:
            raise GdeNotFound(f"腔室 {chamber} 不存在")
        runs, t = [], start.astimezone(ZONE).replace(minute=15, second=0, microsecond=0)
        while t < end:  # 每个整点 :15 和 :45 各一次执行
            for minute in (15, 45):
                at = t.replace(minute=minute)
                if start <= at < end:
                    run = self._make_run(f"RUN-{chamber}-{at:%Y%m%d%H%M}", chamber, at)
                    if run.recipe in recipes:
                        runs.append(run)
            t += timedelta(hours=1)
        return runs

    def run_info(self, run_id):
        self._maybe_fail()
        if run_id.startswith("RUN-MISSING"):
            raise GdeNotFound(f"执行 {run_id} 不存在")
        if m := _RUN_ID.match(run_id):
            end_at = datetime.strptime(m.group(2), "%Y%m%d%H%M").replace(tzinfo=ZONE)
            return self._make_run(run_id, m.group(1), end_at)
        n = _seed(run_id)
        chamber = sorted(self.chambers)[n % len(self.chambers)]
        end_at = datetime(2026, 11, 24, 8, tzinfo=ZONE) + timedelta(minutes=n % 600)
        return self._make_run(run_id, chamber, end_at)

    def trace(self, run_id, signal, unit, timeout_s):
        self.run_info(run_id)  # 不存在时抛 GdeNotFound
        if signal.startswith("MISSING"):
            raise GdeNotFound(f"信号 {signal} 不存在")
        rnd = random.Random(_seed(run_id, signal))
        sparse = _seed(run_id) % 10 == 0
        step = 3 if sparse else 1
        base, slope = rnd.uniform(50, 150), rnd.uniform(0.5, 1.0)
        t = [i / 10 for i in range(0, int(FAKE_STEPS[-1].end_s * 10) + 1, step)]
        v = [base + slope * x + rnd.gauss(0, 0.5) for x in t]
        return t, v


# ====================================================================== 获取客户端
def make_gde(cfg: GdeConfig) -> GdeClient:
    return FakeGdeClient() if cfg.fake else SdkGdeClient(cfg.sdk)


@cache
def get_gde() -> GdeClient:
    """进程内单例。"""
    return make_gde(settings.gde)
