from __future__ import annotations

import asyncio


class Aborted(Exception):
    pass


class Controller:
    def __init__(self):
        self._run = asyncio.Event()
        self._run.set()
        self.abort_reason: str | None = None

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def abort(self, reason: str = "aborted by user") -> None:
        self.abort_reason = self.abort_reason or reason
        self._run.set()  # release a paused runner so it can see the abort

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    async def checkpoint(self) -> None:
        await self._run.wait()
        if self.abort_reason is not None:
            raise Aborted(self.abort_reason)
