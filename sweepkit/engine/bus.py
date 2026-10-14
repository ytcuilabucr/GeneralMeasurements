"""Event bus: each subscriber gets its own bounded queue and drop policy.

`add_sink` runs a consumer as a supervised task: if it raises, its queue is
drained and discarded (so the publisher can never block on a dead sink) and
the failure callbacks fire (the runner aborts the run when a *critical* sink,
e.g. the HDF5 writer, dies: data must never be silently lost).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .events import Event

log = logging.getLogger("sweepkit.bus")
_CLOSED = object()


class Subscription:
    def __init__(self, maxsize: int, policy: str):
        if policy not in ("drop", "block"):
            raise ValueError("policy must be 'drop' or 'block'")
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self.policy, self.dropped, self.dead = policy, 0, False

    async def get(self):
        item = await self.queue.get()
        return None if item is _CLOSED else item

    async def __aiter__(self):
        while (item := await self.get()) is not None:
            yield item

    def kill(self) -> None:
        """Stop accepting items and free any publisher blocked on this queue."""
        self.dead = True
        while not self.queue.empty():
            self.queue.get_nowait()


class Bus:
    def __init__(self):
        self._subs: list[Subscription] = []
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self.failures: list[tuple[str, BaseException, bool]] = []
        self.failure_callbacks: list[Callable[[str, BaseException, bool], None]] = []

    def subscribe(self, maxsize: int = 1000, policy: str = "drop") -> Subscription:
        s = Subscription(maxsize, policy)
        self._subs.append(s)
        return s

    def add_sink(self, name: str, consume: Callable, *, maxsize: int = 1000,
                 policy: str = "block", critical: bool = True) -> asyncio.Task:
        """Run `consume(subscription)` as a supervised task."""
        sub = self.subscribe(maxsize, policy)

        async def supervised():
            try:
                await consume(sub)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("sink %s failed", name)
                sub.kill()
                self.failures.append((name, e, critical))
                for cb in self.failure_callbacks:
                    cb(name, e, critical)
        task = asyncio.get_running_loop().create_task(supervised())
        self._tasks.append(task)
        return task

    async def join(self) -> None:
        """Wait for all supervised sinks to finish (after the bus is closed)."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def publish(self, item) -> None:
        for s in self._subs:
            if s.dead:
                continue
            # drop policy only ever sheds data Events: start/stop/notes always arrive
            if s.policy == "block" or not isinstance(item, Event):
                await s.queue.put(item)
            else:
                try:
                    s.queue.put_nowait(item)
                except asyncio.QueueFull:
                    s.dropped += 1

    async def close(self) -> None:
        """Idempotent: tells every live sink the stream has ended."""
        if self._closed:
            return
        self._closed = True
        for s in self._subs:
            if not s.dead:
                await s.queue.put(_CLOSED)
