"""Redis Stream：读取执行结束通知、确认、认领超时未确认的消息。"""

import os
import socket
from functools import cache

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from src.util.config import CLAIM_IDLE_S, CONSUMER_GROUP, RUN_DONE_STREAM, settings

CONSUMER = f"{socket.gethostname()}-{os.getpid()}"

Message = tuple[str, dict[str, str]]  # (消息 ID, 字段)


@cache
def get_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis.url, decode_responses=True)


class RunDoneStream:
    def __init__(self, r: aioredis.Redis, stream: str = RUN_DONE_STREAM, group: str = CONSUMER_GROUP):
        self.r, self.stream, self.group = r, stream, group

    async def ensure_group(self) -> None:
        """创建消费组（从创建时刻之后的新消息开始）；已存在则忽略。"""
        try:
            await self.r.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read(self, count: int = 32, block_ms: int = 5000) -> list[Message]:
        """读取分给本消费者的新消息。"""
        resp = await self.r.xreadgroup(self.group, CONSUMER, {self.stream: ">"}, count=count, block=block_ms)
        return [m for _, msgs in resp or [] for m in msgs]

    async def claim(self, count: int = 32) -> list[Message]:
        """认领闲置超过 CLAIM_IDLE_S 的未确认消息（原消费者可能已经挂了）。"""
        resp = await self.r.xautoclaim(self.stream, self.group, CONSUMER, CLAIM_IDLE_S * 1000, "0-0", count=count)
        return [m for m in resp[1] if m[1] is not None]

    async def deliveries(self, msg_id: str) -> int:
        """这条消息已投递的次数。"""
        pending = await self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
        return pending[0]["times_delivered"] if pending else 0

    async def ack(self, msg_id: str) -> None:
        await self.r.xack(self.stream, self.group, msg_id)
