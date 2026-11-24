"""后台计算：随服务一起启动、一起停止。"""

import asyncio
import logging

log = logging.getLogger(__name__)


async def _supervise(name: str, make) -> None:
    """后台任务意外退出时记录日志，10 秒后重启。"""
    while True:
        try:
            await make()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("后台任务 %s 异常退出，10 秒后重启", name)
        await asyncio.sleep(10)


def start() -> list[asyncio.Task]:
    from src.engine import batch_runner, jobs
    from src.engine.live import LiveConsumer

    jobs_to_run = {
        "live": lambda: LiveConsumer().run_forever(),
        "batch_runner": batch_runner.run_forever,
        "jobs": jobs.run_forever,
    }
    return [asyncio.create_task(_supervise(name, make), name=name) for name, make in jobs_to_run.items()]


async def stop(tasks: list[asyncio.Task]) -> None:
    from src.engine.sandbox import close_sandbox

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    close_sandbox()
