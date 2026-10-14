"""Async line transport. One transport = one session = one lock (single-owner rule).

`VisaTransport` wraps pyvisa via asyncio.to_thread. `FakeTransport` replays a
dict of canned replies and records every command, for golden-transcript tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol


class DriverError(RuntimeError):
    pass


class Transport(Protocol):
    async def write(self, cmd: str) -> None: ...
    async def query(self, cmd: str) -> str: ...
    async def close(self) -> None: ...


class VisaTransport:
    def __init__(self, resource: str, timeout_ms: int = 5000,
                 read_termination: str = "\n", write_termination: str = "\n"):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.read_termination = read_termination
        self.write_termination = write_termination
        self._inst = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        def _open():
            import pyvisa  # lazy: simulators and tests don't need VISA installed
            inst = pyvisa.ResourceManager().open_resource(self.resource)
            inst.timeout = self.timeout_ms
            inst.read_termination = self.read_termination
            inst.write_termination = self.write_termination
            return inst
        self._inst = await asyncio.to_thread(_open)

    async def _call(self, fn, *args):
        """Run a blocking VISA call under the session lock. If the caller is
        cancelled (abort), keep holding the lock until the call really returns,
        so a following command can never interleave with a half-finished one."""
        async with self._lock:
            fut = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                await asyncio.gather(fut, return_exceptions=True)
                raise

    async def write(self, cmd: str) -> None:
        await self._call(self._inst.write, cmd)

    async def query(self, cmd: str) -> str:
        return (await self._call(self._inst.query, cmd)).strip()

    async def close(self) -> None:
        if self._inst is not None:
            await asyncio.to_thread(self._inst.close)
            self._inst = None


class FakeTransport:
    """Replies: dict of exact command -> reply, or callable(cmd) -> reply."""

    def __init__(self, replies: dict[str, str] | Callable[[str], str] | None = None):
        self.replies = replies or {}
        self.log: list[str] = []

    async def write(self, cmd: str) -> None:
        self.log.append(cmd)

    async def query(self, cmd: str) -> str:
        self.log.append(cmd)
        if callable(self.replies):
            return self.replies(cmd)
        if cmd not in self.replies:
            raise DriverError(f"FakeTransport: no reply for {cmd!r}")
        return self.replies[cmd]

    async def close(self) -> None:
        pass
