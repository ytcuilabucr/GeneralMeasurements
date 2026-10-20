"""Async line transport. One transport = one session = one lock (single-owner rule).

`VisaTransport` wraps pyvisa via asyncio.to_thread. `AotfLibraryTransport`
talks to a Crystal Technology AOTF controller through the vendor's
AotfLibrary.dll. `SerialBytesTransport` exchanges raw bytes on a serial port
(YSL instruments). `FakeTransport` replays a dict of canned replies and records
every command, for golden-transcript tests.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
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


# -- Crystal Technology AOTF controllers (USB, AotfLibrary.dll) -------------------------------
AOTF_DLL = r"C:\Program Files (x86)\Fianium\config files\AotfLibrary.dll"
_AOTF_ERROR = re.compile(r"\b(error|invalid|unknown|illegal|not recognized)\b", re.I)


class AotfPort(Protocol):
    """The controller's byte stream (blocking calls)."""
    def write(self, data: bytes) -> None: ...
    def read_available(self) -> bytes: ...          # all bytes received so far, b"" if none
    def close(self) -> None: ...


class _Server32Port:
    """AotfLibrary.dll is 32-bit only: it runs in msl-loadlib's 32-bit server
    process (aotf_server32.py) and is called from here."""

    def __init__(self, instance: int, dll: str):
        try:
            from msl.loadlib import Client64
        except ImportError:
            raise DriverError("the AOTF needs msl-loadlib (pip install msl-loadlib) to use "
                              "the vendor's 32-bit AotfLibrary.dll") from None
        if not Path(dll).is_file():
            raise DriverError(f"AotfLibrary.dll not found: {dll}")
        self._c = Client64("aotf_server32", append_sys_path=str(Path(__file__).parent),
                           dll=dll)
        if not self._c.request32("open", instance):
            self._c.shutdown_server32()
            raise DriverError(f"no AOTF controller at USB instance {instance} (connected and "
                              "powered? another program using it?)")

    def write(self, data: bytes) -> None:
        self._c.request32("write", data)

    def read_available(self) -> bytes:
        return self._c.request32("read_available")

    def close(self) -> None:
        try:
            self._c.request32("close")
        finally:
            self._c.shutdown_server32()


class AotfLibraryTransport:
    """Commands are ASCII lines ending in CR. After a command the controller sends
    back its output, possibly an echo of the command, and a "*" prompt (AOTF
    Controller Command Reference, rev. 1.3). The vendor's LabVIEW program writes a
    command, waits 0.5 s and reads whatever came. Here a reply ends at the prompt,
    or once the controller has been quiet for `quiet_s`:

    - query(cmd): the reply's lines, without the echo and the prompt. Nothing at
      all within `timeout_s` is a TimeoutError.
    - write(cmd): the same exchange, waiting at most `idle_s` for output. Output
      that reads like an error ("Invalid command ...") raises DriverError.

    `port` replaces the DLL (tests)."""

    def __init__(self, instance: int = 0, dll: str = AOTF_DLL, timeout_s: float = 2.0,
                 quiet_s: float = 0.05, idle_s: float = 0.3,
                 port: Callable[[], AotfPort] | None = None):
        self.resource = f"AOTF::{instance}"
        self.instance, self.dll = instance, dll
        self.timeout_s, self.quiet_s, self.idle_s = timeout_s, quiet_s, idle_s
        self._factory = port or (lambda: _Server32Port(instance, dll))
        self._port: AotfPort | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._port = await asyncio.to_thread(self._factory)

    def _exchange(self, cmd: str, wait_s: float) -> list[str]:
        """Blocking: drop stale output, send `cmd`, collect the reply's lines."""
        port = self._port
        if port is None:
            raise DriverError(f"{self.resource}: not open")
        port.read_available()                            # anything left from before
        port.write(cmd.encode("ascii") + b"\r")
        buf, t0 = b"", time.monotonic()
        last = t0
        while True:
            time.sleep(0.005)
            chunk = port.read_available()
            now = time.monotonic()
            if chunk:
                buf, last = buf + chunk, now
                if buf.rstrip().endswith(b"*"):          # the prompt: the reply is complete
                    break
            elif buf and now - last >= self.quiet_s:
                break
            elif not buf and now - t0 >= wait_s:
                break
        lines = []
        for raw in re.split(r"[\r\n]+", buf.decode("ascii", "replace")):
            line = raw.strip().lstrip("*").strip()
            if line and line.lower() != cmd.strip().lower():
                lines.append(line)
        return lines

    async def _call(self, cmd: str, wait_s: float) -> list[str]:
        async with self._lock:
            fut = asyncio.ensure_future(asyncio.to_thread(self._exchange, cmd, wait_s))
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:              # finish the exchange, then give up
                await asyncio.gather(fut, return_exceptions=True)
                raise

    async def write(self, cmd: str) -> None:
        lines = await self._call(cmd, self.idle_s)
        bad = [x for x in lines if _AOTF_ERROR.search(x)]
        if bad:
            raise DriverError(f"{self.resource}: {cmd!r}: {' / '.join(bad)}")

    async def query(self, cmd: str) -> str:
        lines = await self._call(cmd, self.timeout_s)
        if not lines:
            raise TimeoutError(f"{self.resource}: no reply to {cmd!r}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._port is not None:
            port, self._port = self._port, None
            await asyncio.to_thread(port.close)


# -- raw serial bytes (YSL instruments) --------------------------------------------------------
def serial_resource(port: str) -> str:
    """'COM5', 'ASRL5' or 'ASRL5::INSTR' -> 'ASRL5::INSTR' (ValueError otherwise)."""
    m = re.fullmatch(r"(?:COM|ASRL)(\d+)(?:::INSTR)?", port.strip().upper())
    if not m:
        raise ValueError(f"serial port {port!r}: use 'COM<n>' or 'ASRL<n>::INSTR'")
    return f"ASRL{m.group(1)}::INSTR"


class _VisaSerialPort:
    """A COM port through VISA (NI-VISA here), 8N1, no termination: raw bytes."""

    def __init__(self, resource: str, baud: int):
        import pyvisa  # lazy: tests don't need VISA
        from pyvisa import constants
        try:
            inst = pyvisa.ResourceManager().open_resource(resource)
        except pyvisa.errors.VisaIOError as e:
            raise DriverError(f"{resource}: {e}") from None
        inst.baud_rate, inst.data_bits = baud, 8
        inst.parity, inst.stop_bits = constants.Parity.none, constants.StopBits.one
        inst.read_termination = inst.write_termination = None
        inst.end_input = constants.SerialTermination.none
        inst.timeout = 1000
        self._inst = inst

    def write(self, data: bytes) -> None:
        self._inst.write_raw(data)

    def read_available(self) -> bytes:
        n = self._inst.bytes_in_buffer
        return bytes(self._inst.read_bytes(n)) if n else b""

    def close(self) -> None:
        self._inst.close()


class SerialBytesTransport:
    """Raw bytes on a serial port, for instruments whose replies have no reliable
    terminator (the YSL AOTF answers in binary; the SC-PRO8 starts some replies
    with CR). exchange(data) drops stale input, writes, and collects the reply
    until the port has been quiet for `quiet_s` after the first byte, or for
    `wait_s` when nothing comes (b""). `port` replaces VISA (tests)."""

    def __init__(self, resource: str, baud: int = 9600, wait_s: float = 1.0,
                 quiet_s: float = 0.15, port: Callable[[], AotfPort] | None = None):
        self.resource = serial_resource(resource)
        self.baud, self.wait_s, self.quiet_s = baud, wait_s, quiet_s
        self._factory = port or (lambda: _VisaSerialPort(self.resource, baud))
        self._port: AotfPort | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._port = await asyncio.to_thread(self._factory)

    def _exchange(self, data: bytes, wait_s: float) -> bytes:
        port = self._port
        if port is None:
            raise DriverError(f"{self.resource}: not open")
        port.read_available()                            # anything left from before
        port.write(data)
        buf, t0 = b"", time.monotonic()
        last = t0
        while True:
            time.sleep(0.01)
            chunk = port.read_available()
            now = time.monotonic()
            if chunk:
                buf, last = buf + chunk, now
            elif buf and now - last >= self.quiet_s:
                return buf
            elif not buf and now - t0 >= wait_s:
                return b""

    async def exchange(self, data: bytes, wait_s: float | None = None) -> bytes:
        async with self._lock:
            fut = asyncio.ensure_future(asyncio.to_thread(
                self._exchange, data, self.wait_s if wait_s is None else wait_s))
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:              # finish the exchange, then give up
                await asyncio.gather(fut, return_exceptions=True)
                raise

    async def close(self) -> None:
        if self._port is not None:
            port, self._port = self._port, None
            await asyncio.to_thread(port.close)


class FakeSerial:
    """A SerialBytesTransport stand-in: `reply(data) -> bytes`; records `log`."""

    def __init__(self, reply: Callable[[bytes], bytes] | None = None):
        self.reply = reply or (lambda data: b"")
        self.resource = "ASRL0::INSTR"
        self.log: list[bytes] = []

    async def exchange(self, data: bytes, wait_s: float | None = None) -> bytes:
        self.log.append(data)
        return self.reply(data)

    async def close(self) -> None:
        pass
