"""Notifier sink: forwards run start/stop and operator notes to a `send(text)` coroutine.

Any transport (Slack bot, email) plugs in as `send`. `webhook_sender` posts JSON
{"text": ...} to a URL using only the stdlib. Send failures are logged, never
raised: a dead notifier must not disturb the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable

from ..engine.events import Note, RunStart, RunStop

log = logging.getLogger("gmcore.notify")


def webhook_sender(url: str, timeout: float = 10.0) -> Callable[[str], Awaitable[None]]:
    def _post(text: str) -> None:
        req = urllib.request.Request(url, json.dumps({"text": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout).close()

    async def send(text: str) -> None:
        await asyncio.to_thread(_post, text)
    return send


class NotifySink:
    def __init__(self, send: Callable[[str], Awaitable[None]], station: str = "",
                 notify_success: bool = True, notify_start: bool = False):
        self.send, self.station = send, station
        self.notify_success, self.notify_start = notify_success, notify_start

    async def _say(self, text: str) -> None:
        try:
            await self.send(f"[{self.station}] {text}" if self.station else text)
        except Exception:  # noqa: BLE001
            log.exception("notification failed")

    async def consume(self, sub) -> None:
        uid = ""
        async for item in sub:
            if isinstance(item, RunStart):
                uid = item.uid
                if self.notify_start:
                    await self._say(f"run {uid} started, shape {item.shape}")
            elif isinstance(item, Note):
                await self._say(f"run {uid}: {item.message}")
            elif isinstance(item, RunStop):
                if item.status != "success" or self.notify_success:
                    tail = f": {item.reason}" if item.reason else ""
                    await self._say(f"run {uid} {item.status}{tail}")
