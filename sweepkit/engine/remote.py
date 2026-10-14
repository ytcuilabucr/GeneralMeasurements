"""Remote control: a line-based TCP server inside the process that owns the instruments.

Protocol: one command per line -> one JSON line back.
    pause | resume | abort [reason...] | status | ping
Binds to 127.0.0.1 by default and has no authentication: do not expose it to
a network. Other services (Slack bot, CLI) talk to this, never to instruments.
"""

from __future__ import annotations

import asyncio
import json
import math

from .control import Controller
from .events import Event, Note, RunStart, RunStop


class StatusTracker:
    """Bus consumer that keeps a small status dict for the control server."""

    def __init__(self, controller: Controller):
        self.controller = controller
        self.state: dict = {"state": "idle", "uid": None, "seq": None,
                            "total": None, "reason": "", "last_note": ""}

    async def consume(self, sub) -> None:
        async for item in sub:
            if isinstance(item, RunStart):
                stepped = getattr(item.plan, "mode", "stepped") == "stepped"
                total = math.prod(item.shape) if stepped and item.shape else None
                self.state.update(state="running", uid=item.uid, seq=None, total=total,
                                  reason="", last_note="")
            elif isinstance(item, Event):
                self.state["seq"] = item.seq
            elif isinstance(item, Note):
                self.state["last_note"] = item.message
            elif isinstance(item, RunStop):
                self.state.update(state=item.status, reason=item.reason)

    def snapshot(self) -> dict:
        s = dict(self.state)
        if s["state"] == "running" and self.controller.paused:
            s["state"] = "paused"
        return s


class ControlServer:
    def __init__(self, controller: Controller, tracker: StatusTracker | None = None,
                 host: str = "127.0.0.1", port: int = 8765):
        self.controller, self.tracker = controller, tracker
        self.host, self.port = host, port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]   # resolves port 0

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def execute(self, line: str) -> dict:
        cmd, _, arg = line.strip().partition(" ")
        cmd = cmd.lower()
        if cmd == "ping":
            return {"ok": True}
        if cmd == "pause":
            self.controller.pause()
        elif cmd == "resume":
            self.controller.resume()
        elif cmd == "abort":
            self.controller.abort(arg.strip() or "aborted remotely")
        elif cmd != "status":
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        out = {"ok": True}
        if self.tracker:
            out.update(self.tracker.snapshot())
        else:
            out["paused"] = self.controller.paused
        return out

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                if len(line) > 1024:
                    reply = {"ok": False, "error": "line too long"}
                else:
                    reply = self.execute(line.decode("utf-8", "replace"))
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


async def send_command(command: str, host: str = "127.0.0.1", port: int = 8765,
                       timeout: float = 5.0) -> dict:
    """Client side, used by the CLI."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        writer.write((command.strip() + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), timeout))
    finally:
        writer.close()
