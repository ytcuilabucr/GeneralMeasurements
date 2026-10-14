"""Command line: validate a station, run a plan file, control a running scan.

    sweepkit validate station.toml
    sweepkit run plans/iv.py --station station.toml --out data --sample S1
    sweepkit ctl pause|resume|abort|status [reason] [--port 8765]

A plan file is a Python file that defines SCAN (a Scan or FlyScan) and may
define RETRY (a RetryPolicy). `run` owns the instruments for the whole run and
serves pause/resume/abort on a localhost port; `ctl` talks to that port.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path


def _load_plan(path: str):
    spec = importlib.util.spec_from_file_location("sweepkit_plan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "SCAN"):
        raise SystemExit(f"{path}: plan file must define SCAN")
    return mod


async def _run(args) -> int:
    from .config import load_station
    from .config.registry import build_devices, close_devices
    from .engine import Bus, Controller, LoopLagMonitor, run
    from .engine.remote import ControlServer, StatusTracker
    from .provenance import collect
    from .sinks import HDF5Sink, NotifySink, webhook_sender

    cfg = load_station(args.station)
    mod = _load_plan(args.plan)
    scan = mod.SCAN
    devices = await build_devices(cfg)
    ctl, bus = Controller(), Bus()
    tracker = StatusTracker(ctl)
    server = ControlServer(ctl, tracker, args.host, args.port)
    lag = LoopLagMonitor()
    out = Path(args.out) / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.sample}.h5"
    tasks = [asyncio.create_task(HDF5Sink(out).consume(bus.subscribe(policy="block"))),
             asyncio.create_task(tracker.consume(bus.subscribe(policy="block")))]
    if args.webhook:
        notifier = NotifySink(webhook_sender(args.webhook), cfg.station.name)
        tasks.append(asyncio.create_task(notifier.consume(bus.subscribe(policy="block"))))
    try:
        await server.start()
        lag.start()
        print(f"control port {server.port}; writing {out}", flush=True)
        stop = await run(scan, devices, bus, ctl, cfg.station.shutdown_order,
                         provenance={**collect(args.station), "sample": args.sample},
                         retry=getattr(mod, "RETRY", None))
        await asyncio.gather(*tasks)
        print(f"{stop.status}{': ' + stop.reason if stop.reason else ''}")
        return 0 if stop.status == "success" else 1
    finally:
        await lag.stop()
        await server.stop()
        await close_devices(devices)


async def _ctl(args) -> int:
    from .engine.remote import send_command
    cmd = args.command + (" " + " ".join(args.reason) if args.reason else "")
    try:
        reply = await send_command(cmd, args.host, args.port)
    except (OSError, asyncio.TimeoutError) as e:
        print(f"cannot reach run at {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sweepkit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a station TOML (touches no hardware)")
    v.add_argument("station")

    r = sub.add_parser("run", help="run a plan file")
    r.add_argument("plan")
    r.add_argument("--station", required=True)
    r.add_argument("--out", default="data")
    r.add_argument("--sample", default="run")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--webhook", help="URL to POST notifications to")

    c = sub.add_parser("ctl", help="pause/resume/abort/status of a running scan")
    c.add_argument("command", choices=["pause", "resume", "abort", "status"])
    c.add_argument("reason", nargs="*")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.cmd == "validate":
        from .config import load_station
        cfg = load_station(args.station)
        print(f"ok: station {cfg.station.name!r}, devices: {', '.join(cfg.devices)}")
        return 0
    return asyncio.run(_run(args) if args.cmd == "run" else _ctl(args))


if __name__ == "__main__":
    raise SystemExit(main())
