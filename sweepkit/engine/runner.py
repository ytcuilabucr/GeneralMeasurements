"""Stepped runner: checkpoint, move changed axes, settle, read concurrently, emit."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping

from ..plans import Scan
from .bus import Bus
from .control import Aborted, Controller
from .events import Event, RunStart, RunStop

log = logging.getLogger("sweepkit.runner")


async def safe_shutdown(devices: Mapping[str, object], order: list[str]) -> list[str]:
    """Call safe_state() on every device that has one: `order` first, then the rest.
    One failure never prevents the others. Returns error messages."""
    names = [n for n in order if n in devices] + [n for n in devices if n not in order]
    errors = []
    for n in names:
        fn = getattr(devices[n], "safe_state", None)
        if fn is None:
            continue
        try:
            await fn()
        except Exception as e:  # noqa: BLE001
            log.exception("safe_state failed for %s", n)
            errors.append(f"{n}: {e}")
    return errors


async def _snapshots(devices: Mapping[str, object]) -> dict:
    out = {}
    for n, d in devices.items():
        fn = getattr(d, "snapshot", None)
        out[n] = await fn() if fn else {}
    return out


async def run(scan: Scan, devices: Mapping[str, object], bus: Bus,
              controller: Controller | None = None,
              shutdown_order: list[str] | None = None,
              provenance: Mapping | None = None,
              start_seq: int = 0) -> RunStop:
    controller = controller or Controller()
    scan.validate(devices)            # PlanError here: nothing has moved yet
    uid = uuid.uuid4().hex[:12]
    axis_names = [a.device for a in scan.axes]
    read_names = list(dict.fromkeys(axis_names + list(scan.readables)))
    fields = {}
    for n in read_names:
        desc = getattr(devices[n], "describe", None)
        if desc:
            fields.update(desc())

    stop = RunStop("failed", "not started")
    error: BaseException | None = None
    try:
        await bus.publish(RunStart(uid, scan, scan.shape, fields,
                                   await _snapshots(devices),
                                   {**(provenance or {}), "start_seq": start_seq}))
        prev: dict[str, float] = {}
        for seq, idx, sp in scan.points():
            if seq < start_seq:   # resume: skip completed points
                continue
            await controller.checkpoint()
            moved = [a for a in scan.axes if prev.get(a.device) != sp[a.device]]
            if moved:
                await asyncio.gather(*(devices[a.device].set(sp[a.device]) for a in moved))
                settle = max(a.settle_s for a in moved)
                if settle:
                    await asyncio.sleep(settle)
            prev = sp
            results = await asyncio.gather(*(devices[n].read() for n in read_names))
            data: dict = {}
            for r in results:
                data.update(r)
            ev = Event(seq, idx, dict(sp), data)
            await bus.publish(ev)
            for wd in scan.watchdogs:
                reason = await wd(ev)
                if reason:
                    controller.abort(f"watchdog: {reason}")
        stop = RunStop("success")
    except Aborted as e:
        stop = RunStop("aborted", str(e))
    except asyncio.CancelledError as e:
        stop, error = RunStop("aborted", "cancelled"), e
    except Exception as e:  # noqa: BLE001
        stop, error = RunStop("failed", f"{type(e).__name__}: {e}"), e
    finally:
        errs = await asyncio.shield(safe_shutdown(devices, shutdown_order or []))
        if errs and stop.status == "success":
            stop = RunStop("failed", "safe_state errors: " + "; ".join(errs))
        await asyncio.shield(bus.publish(stop))
        await asyncio.shield(bus.close())
    if error is not None:
        raise error
    return stop
