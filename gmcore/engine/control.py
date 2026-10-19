from __future__ import annotations

import asyncio
from collections.abc import Coroutine


class Aborted(Exception):
    pass


class Controller:
    def __init__(self):
        self._run = asyncio.Event()
        self._run.set()
        self._abort = asyncio.Event()
        self.abort_reason: str | None = None

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def abort(self, reason: str = "aborted by user") -> None:
        self.abort_reason = self.abort_reason or reason
        self._abort.set()
        self._run.set()  # release a paused runner so it can see the abort

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    async def checkpoint(self) -> None:
        await self._run.wait()
        if self.abort_reason is not None:
            raise Aborted(self.abort_reason)

    async def guard(self, coro: Coroutine):
        """Await `coro`, but cancel it and raise Aborted the moment abort() is called.

        Lets an abort interrupt a long move or ramp wait instead of waiting for
        the next point boundary. Pause is deliberately not interrupting.
        """
        if self.abort_reason is not None:
            coro.close()
            raise Aborted(self.abort_reason)
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(self._abort.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            waiter.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
            raise
        waiter.cancel()
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise Aborted(self.abort_reason)
