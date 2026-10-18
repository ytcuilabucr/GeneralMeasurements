"""Command line: validate a station, dry-run a plan, run it, control a running scan.

    gm validate station.toml
    gm dry-run plans/iv.py --station station.toml [--read-s 0.5]
    gm run plans/iv.py --station station.toml --out data --sample S1
    gm ctl pause|resume|abort|status [reason] [--port 8765]

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
from types import SimpleNamespace


def _load_plan(path: str):
    spec = importlib.util.spec_from_file_location("generalmeasurements_plan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "SCAN"):
        raise SystemExit(f"{path}: plan file must define SCAN")
    return mod


async def _run(args) -> int:
    from .api import run_scan
    from .config import load_station
    from .config.registry import build_devices, close_devices
    from .provenance import collect
    from .sinks import NotifySink, webhook_sender

    cfg = load_station(args.station)
    mod = _load_plan(args.plan)
    if args.plot is not None:       # fail fast, before any instrument is opened
        from .sinks.liveplot import _require_pyqtgraph
        _require_pyqtgraph()
    devices = await build_devices(cfg)
    out = Path(args.out) / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.sample}.h5"
    sinks = []
    if args.plot is not None:
        from .sinks import LivePlotSink
        fields = [f for f in args.plot.split(",") if f] or None
        sinks.append(("liveplot", LivePlotSink(fields, args.plot_x, title=args.sample,
                                               keep_open=args.plot_keep).consume))
    if args.webhook:
        notifier = NotifySink(webhook_sender(args.webhook), cfg.station.name)
        sinks.append(("notify", notifier.consume))
    try:
        stop = await run_scan(
            mod.SCAN, devices, out=out, shutdown_order=cfg.station.shutdown_order,
            provenance={**collect(args.station), "sample": args.sample},
            retry=getattr(mod, "RETRY", None), sinks=sinks,
            serve_port=args.port, serve_host=args.host,
            on_server_ready=lambda port: print(f"control port {port}; writing {out}",
                                               flush=True))
        print(f"{stop.status}{': ' + stop.reason if stop.reason else ''}")
        return 0 if stop.status == "success" else 1
    finally:
        await close_devices(devices)


def _dry_run(args) -> int:
    """Validate a plan against the station's configured limits and estimate its
    duration. Builds stand-ins from the TOML only: no instrument is opened."""
    from .config import load_station

    cfg = load_station(args.station)
    scan = _load_plan(args.plan).SCAN
    stubs = {}
    for name, d in cfg.devices.items():
        attrs = {"read": None, "describe": lambda: {}}
        if hasattr(d, "limits"):
            attrs.update(set=None, limits=tuple(d.limits))
            if d.driver in ("sim_magnet", "lakeshore_335", "lakeshore_625"):
                attrs.update(start_ramp=None, ramp_done=None)
        stubs[name] = SimpleNamespace(**attrs)
    scan.validate(stubs)
    print(f"plan ok: mode={scan.mode}, shape={tuple(scan.shape)}, "
          f"axes={list(scan.axis_names)}")
    if scan.mode == "stepped":
        est = scan.estimate_time(read_s=args.read_s, move_s=args.move_s)
        print(f"points: {scan.n_points}")
    else:
        est = scan.estimate_time(move_s=args.move_s)
        print(f"ramps: {scan.n_ramps}, ramp time each: {scan.duration_s:.1f} s")
    h, rem = divmod(est, 3600)
    print(f"estimated duration: {int(h)} h {int(rem // 60)} min {rem % 60:.0f} s "
          f"(read_s={args.read_s}, move_s={args.move_s})")
    return 0


async def _ctl(args) -> int:
    from .engine.remote import send_command
    cmd = args.command + (" " + " ".join(args.reason) if args.reason else "")
    try:
        reply = await send_command(cmd, args.host, args.port)
    except (TimeoutError, OSError) as e:
        print(f"cannot reach run at {args.host}:{args.port}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gm")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a station TOML (touches no hardware)")
    v.add_argument("station")

    d = sub.add_parser("dry-run", help="validate a plan and estimate its duration (no hardware)")
    d.add_argument("plan")
    d.add_argument("--station", required=True)
    d.add_argument("--read-s", type=float, default=0.0, help="seconds per point for reads")
    d.add_argument("--move-s", type=float, default=0.0, help="seconds per move (excl. settle)")

    r = sub.add_parser("run", help="run a plan file")
    r.add_argument("plan")
    r.add_argument("--station", required=True)
    r.add_argument("--out", default="data")
    r.add_argument("--sample", default="run")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--webhook", help="URL to POST notifications to")
    r.add_argument("--plot", nargs="?", const="", metavar="FIELDS",
                   help="live plot (comma-separated fields; default: first few)")
    r.add_argument("--plot-x", help="x axis for the live plot (axis name or data field)")
    r.add_argument("--plot-keep", action="store_true",
                   help="leave the plot window open after the run")

    c = sub.add_parser("ctl", help="pause/resume/abort/status of a running scan")
    c.add_argument("command", choices=["pause", "resume", "abort", "status"])
    c.add_argument("reason", nargs="*")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8765)

    g = sub.add_parser("gui", help="instrument control windows, one per instrument type "
                                   "(connecting resets nothing)")
    g.add_argument("--station", required=True)
    g.add_argument("--reset-layout", action="store_true",
                   help="forget the saved window and dock layout for this station")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.cmd == "gui":
        try:
            from .gui.app import main as gui_main
        except ImportError as e:
            print(f"the GUI needs PySide6, PySide6-QtAds and pyqtgraph: pip install -e .[gui] "
                  f"({e})", file=sys.stderr)
            return 2
        return gui_main(args.station, reset_layout=args.reset_layout)
    if args.cmd == "validate":
        from .config import load_station
        cfg = load_station(args.station)
        print(f"ok: station {cfg.station.name!r}, devices: {', '.join(cfg.devices)}")
        return 0
    if args.cmd == "dry-run":
        return _dry_run(args)
    return asyncio.run(_run(args) if args.cmd == "run" else _ctl(args))


if __name__ == "__main__":
    raise SystemExit(main())
