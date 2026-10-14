"""Runner: shared run lifecycle plus the stepped body (fly body is in fly.py).

Lifecycle for both modes: validate, RunStart, body, then in `finally` shielded
safe_shutdown and RunStop (success / aborted / failed) and bus close.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping

from ..plans import PlanError, Scan
from .bus import Bus
from .control import Aborted, Controller
from .events import Event, Note, RunStart, RunStop
from .fly import fly_body
from .retry import RetryPolicy

log = logging.getLogger("sweepkit.runner")


async def safe_shutdown(devices: Mapping[str, object], order: list[str],
                        timeout_s: float = 60.0) -> list[str]:
    """Call safe_state() on every device that has one: `order` first, then the rest.
    One failure or hang (bounded by `timeout_s`) never prevents the others.
    Returns error messages."""
    names = [n for n in order if n in devices] + [n for n in devices if n not in order]
    errors = []
    for n in names:
        fn = getattr(devices[n], "safe_state", None)
        if fn is None:
            continue
        try:
            await asyncio.wait_for(fn(), timeout_s)
        except Exception as e:  # noqa: BLE001
            log.exception("safe_state failed for %s", n)
            errors.append(f"{n}: {type(e).__name__}: {e}")
    return errors


async def _snapshots(devices: Mapping[str, object]) -> dict:
    out = {}
    for n, d in devices.items():
        fn = getattr(d, "snapshot", None)
        out[n] = await fn() if fn else {}
    return out


async def _stepped_body(scan: Scan, devices, emit, note, controller: Controller,
                        read_names: list[str], start_seq: int, retry: RetryPolicy) -> None:
    prev: dict[str, float] = {}

    async def acquire(sp) -> dict:
        nonlocal prev
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
        return data

    for seq, idx, sp in scan.points():
        if seq < start_seq:   # resume: skip completed points
            continue
        await controller.checkpoint()
        attempt = 0
        while True:
            try:
                data = await asyncio.wait_for(acquire(sp), retry.point_timeout_s)
                break
            except retry.retry_on as e:
                prev = {}     # position unknown: re-move every axis on the retry
                attempt += 1
                if attempt <= retry.retries:
                    await note(f"seq {seq}: {type(e).__name__}: {e}; "
                               f"retry {attempt}/{retry.retries}")
                    if retry.backoff_s:
                        await asyncio.sleep(retry.backoff_s)
                    continue
                if retry.on_exhausted == "pause":
                    await note(f"seq {seq}: {type(e).__name__}: {e}; retries exhausted, "
                               "paused: waiting for operator (resume or abort)")
                    controller.pause()
                    await controller.checkpoint()   # blocks; raises Aborted on abort
                    attempt = 0
                    continue
                raise
        await emit(Event(seq, idx, dict(sp), data))


async def run(scan, devices: Mapping[str, object], bus: Bus,
              controller: Controller | None = None,
              shutdown_order: list[str] | None = None,
              provenance: Mapping | None = None,
              start_seq: int = 0,
              retry: RetryPolicy | None = None,
              shutdown_timeout_s: float = 60.0) -> RunStop:
    """Run a Scan (stepped) or FlyScan (continuous). Returns the RunStop."""
    controller = controller or Controller()
    retry = retry or RetryPolicy()
    scan.validate(devices)            # PlanError here: nothing has moved yet
    if scan.mode == "fly" and start_seq:
        raise ValueError("resume is not supported for fly scans")
    uid = uuid.uuid4().hex[:12]
    read_names = list(dict.fromkeys((*scan.axis_names, *scan.readables)))
    fields: dict = {}
    owner: dict[str, str] = {}
    for n in read_names:
        desc = getattr(devices[n], "describe", None)
        for key, info in (desc() if desc else {}).items():
            if key in owner:       # the flat data dict would silently overwrite one
                raise PlanError(f"field {key!r} is produced by both {owner[key]!r} and {n!r}")
            owner[key], fields[key] = n, info

    async def note(msg: str) -> None:
        log.warning(msg)
        await bus.publish(Note(msg))

    async def emit(ev: Event) -> None:
        await bus.publish(ev)
        for wd in scan.watchdogs:
            reason = await wd(ev)
            if reason:
                await note(f"watchdog tripped: {reason}")
                controller.abort(f"watchdog: {reason}")

    def on_sink_failure(name: str, exc: BaseException, critical: bool) -> None:
        if critical:
            controller.abort(f"sink {name!r} failed: {type(exc).__name__}: {exc}")
    bus.failure_callbacks.append(on_sink_failure)
    for name, exc, critical in bus.failures:      # failed before the run began
        on_sink_failure(name, exc, critical)

    stop = RunStop("failed", "not started")
    error: BaseException | None = None
    try:
        await bus.publish(RunStart(uid, scan, scan.shape, fields,
                                   await _snapshots(devices),
                                   {**(provenance or {}), "start_seq": start_seq}))
        for n in read_names:      # only devices this scan uses (e.g. outputs ON at 0)
            prep = getattr(devices[n], 'prepare', None)
            if prep:
                await prep()
        # guard(): abort() cancels the body at once (e.g. mid-ramp), not at the next point
        if scan.mode == "fly":
            body = fly_body(scan, devices, emit, controller)
        else:
            body = _stepped_body(scan, devices, emit, note, controller, read_names,
                                 start_seq, retry)
        await controller.guard(body)
        # a watchdog may have tripped on the very last point
        stop = (RunStop("aborted", controller.abort_reason)
                if controller.abort_reason else RunStop("success"))
    except Aborted as e:
        stop = RunStop("aborted", str(e))
    except asyncio.CancelledError as e:
        stop, error = RunStop("aborted", "cancelled"), e
    except Exception as e:  # noqa: BLE001
        stop, error = RunStop("failed", f"{type(e).__name__}: {e}"), e
    finally:
        errs = await asyncio.shield(safe_shutdown(devices, shutdown_order or [],
                                                       shutdown_timeout_s))
        if errs and stop.status == "success":
            stop = RunStop("failed", "safe_state errors: " + "; ".join(errs))
        await asyncio.shield(bus.publish(stop))
        await asyncio.shield(bus.close())
    if error is not None:
        raise error
    return stop
