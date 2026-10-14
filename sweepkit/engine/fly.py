"""Fly body: move to start, start one ramp, sample every dt until the ramp is done.

Emits the same Event type as the stepped body. Event.setpoints holds the
nominal (time-interpolated) position; the true value is the axis readback in
Event.data. Pausing holds the ramp at the current readback and re-ramps to the
target on resume, so a pause never lets the axis run away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from ..plans import FlyScan
from .control import Controller
from .events import Event


def _readback(data: dict, name: str) -> float:
    """Axis readback: key == device name, else the first scalar field."""
    if name in data:
        return float(data[name])
    for v in data.values():
        return float(v)
    raise RuntimeError(f"{name}: device returned no readback")


async def fly_body(scan: FlyScan, devices: Mapping[str, object],
                   emit: Callable[[Event], Awaitable[None]],
                   controller: Controller) -> None:
    dev = devices[scan.device]
    names = list(dict.fromkeys((scan.device, *scan.readables)))
    loop = asyncio.get_running_loop()

    await dev.set(scan.start)                      # returns when settled
    if scan.settle_s:
        await asyncio.sleep(scan.settle_s)
    await controller.checkpoint()

    sign = 1.0 if scan.stop >= scan.start else -1.0
    await dev.start_ramp(scan.stop, scan.rate)
    seg_t0, seg_v0 = loop.time(), scan.start
    deadline = seg_t0 + (scan.timeout_s if scan.timeout_s is not None
                         else 1.5 * scan.duration_s + 10.0)
    next_t, seq = seg_t0, 0

    while True:
        if controller.paused:
            held_at = loop.time()
            cur = _readback(await dev.read(), scan.device)
            await dev.start_ramp(cur, scan.rate)   # hold where we are
            await controller.checkpoint()          # blocks; raises Aborted
            await dev.start_ramp(scan.stop, scan.rate)
            seg_t0, seg_v0 = loop.time(), cur
            deadline += seg_t0 - held_at
            next_t = seg_t0
        await controller.checkpoint()

        done = await dev.ramp_done()               # before sampling: last sample is post-ramp
        results = await asyncio.gather(*(devices[n].read() for n in names))
        data: dict = {}
        for r in results:
            data.update(r)
        travelled = min(scan.rate * (loop.time() - seg_t0), abs(scan.stop - seg_v0))
        nominal = scan.stop if done else seg_v0 + sign * travelled
        await emit(Event(seq, (seq,), {scan.device: nominal}, data))
        seq += 1
        if done:
            return
        if loop.time() > deadline:
            raise TimeoutError(f"{scan.device}: ramp not done after timeout")
        next_t += scan.dt
        await asyncio.sleep(max(0.0, next_t - loop.time()))
