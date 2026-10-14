"""Event bus: each subscriber gets its own bounded queue and drop policy."""

from __future__ import annotations

import asyncio

_CLOSED = object()


class Subscription:
    def __init__(self, maxsize: int, policy: str):
        if policy not in ("drop", "block"):
            raise ValueError("policy must be 'drop' or 'block'")
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self.policy, self.dropped = policy, 0

    async def get(self):
        item = await self.queue.get()
        return None if item is _CLOSED else item

    async def __aiter__(self):
        while (item := await self.get()) is not None:
            yield item


class Bus:
    def __init__(self):
        self._subs: list[Subscription] = []

    def subscribe(self, maxsize: int = 1000, policy: str = "drop") -> Subscription:
        s = Subscription(maxsize, policy)
        self._subs.append(s)
        return s

    async def publish(self, item) -> None:
        for s in self._subs:
            if s.policy == "block":
                await s.queue.put(item)
            else:
                try:
                    s.queue.put_nowait(item)
                except asyncio.QueueFull:
                    s.dropped += 1

    async def close(self) -> None:
        for s in self._subs:
            await s.queue.put(_CLOSED)
