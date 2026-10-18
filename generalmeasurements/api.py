"""Scripting entry point: wire sinks, control server and run in one call.

    stop = await run_scan(scan, devices, out="data/run.h5",
                          shutdown_order=["gate", "bias"], serve_port=8765)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from .engine import Bus, Controller, LoopLagMonitor, RetryPolicy, RunStop, run
from .engine.remote import ControlServer, StatusTracker
from .sinks import HDF5Sink


async def run_scan(scan, devices: Mapping[str, object], *,
                   out: str | Path | None = None,
                   shutdown_order: list[str] | None = None,
                   provenance: Mapping | None = None,
                   retry: RetryPolicy | None = None,
                   controller: Controller | None = None,
                   sinks: Iterable[tuple[str, Callable]] = (),
                   serve_port: int | None = None, serve_host: str = "127.0.0.1",
                   resume: bool = False, start_seq: int = 0,
                   flush_every: int = 10,
                   on_server_ready: Callable[[int], None] | None = None) -> RunStop:
    """Run `scan` with the standard wiring.

    out         HDF5 path (critical sink: if it fails the run aborts, never silently
                continues without data). None = no file.
    sinks       extra (name, consume) pairs; non-critical, so a broken notifier or
                plot never aborts a run.
    serve_port  start the pause/resume/abort server on this port (0 = ephemeral).
    """
    controller = controller or Controller()
    bus = Bus()
    if out is not None:
        sink = HDF5Sink(out, flush_every=flush_every, resume=resume)
        bus.add_sink("hdf5", sink.consume, policy="block", critical=True)
    for name, consume in sinks:
        bus.add_sink(name, consume, policy="drop", critical=False)

    server = lag = None
    try:
        if serve_port is not None:
            tracker = StatusTracker(controller)
            bus.add_sink("status", tracker.consume, policy="block", critical=False)
            server = ControlServer(controller, tracker, serve_host, serve_port)
            await server.start()
            if on_server_ready:
                on_server_ready(server.port)
        lag = LoopLagMonitor()
        lag.start()
        try:
            return await run(scan, devices, bus, controller, shutdown_order,
                             provenance=provenance, start_seq=start_seq, retry=retry)
        finally:
            await bus.close()      # no-op if run() already closed it (e.g. PlanError)
            await bus.join()
    finally:
        if lag:
            await lag.stop()
        if server:
            await server.stop()
