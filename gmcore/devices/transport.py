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
    """`echo`: the instrument repeats each command line before replying (RS-232
    on Cryomagnetics meters). Queries skip a line equal to the command; writes
    read and drop the echo, waiting at most `echo_wait_ms` for it."""

    def __init__(self, resource: str, timeout_ms: int = 5000,
                 read_termination: str = "\n", write_termination: str = "\n",
                 echo: bool = False, echo_wait_ms: int = 500):
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.read_termination = read_termination
        self.write_termination = write_termination
        self.echo, self.echo_wait_ms = echo, echo_wait_ms
        self._last_write = ""
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

    def _guarded(self, fn, cmd: str):
        """Blocking. Map VISA errors onto the retryable DriverError/TimeoutError.
        On a timeout, device-clear first: otherwise a reply that arrives late
        is read by the next query (seen on a Keithley 2000: '-KEITHLEY...')."""
        import pyvisa
        try:
            return fn(cmd)
        except pyvisa.errors.VisaIOError as e:
            if e.error_code == pyvisa.constants.StatusCode.error_timeout:
                try:
                    self._inst.clear()
                except pyvisa.errors.VisaIOError:
                    pass
                raise TimeoutError(f"{self.resource}: {cmd!r} timed out") from e
            raise DriverError(f"{self.resource}: {cmd!r}: {e}") from e

    def _write_echo(self, cmd: str) -> None:
        """Blocking: write, then drop the echo if it comes (late echoes are
        skipped by the next query)."""
        import pyvisa
        self._inst.write(cmd)
        old = self._inst.timeout
        self._inst.timeout = self.echo_wait_ms
        try:
            self._inst.read()
        except pyvisa.errors.VisaIOError:
            pass
        finally:
            self._inst.timeout = old

    def _query_echo(self, cmd: str) -> str:
        """Blocking: write, then read until a line that isn't an echoed command."""
        self._inst.write(cmd)
        line = self._inst.read()
        for _ in range(3):
            if line.strip() not in (cmd.strip(), self._last_write):
                break
            line = self._inst.read()
        return line

    async def write(self, cmd: str) -> None:
        if self.echo:
            self._last_write = cmd.strip()
            await self._call(self._guarded, self._write_echo, cmd)
        else:
            await self._call(self._guarded, self._inst.write, cmd)

    async def query(self, cmd: str) -> str:
        fn = self._query_echo if self.echo else self._inst.query
        return (await self._call(self._guarded, fn, cmd)).strip()

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
