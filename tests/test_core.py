import asyncio

import h5py
import numpy as np
import pytest

from sweepkit import cli
from sweepkit.api import run_scan
from sweepkit.devices.sim import Fault, FaultyDevice, SimLockin, SimMagnet, SimSource
from sweepkit.engine import Bus, Controller, Event, RetryPolicy, RunStart, RunStop, run
from sweepkit.engine.remote import send_command
from sweepkit.load import load
from sweepkit.plans import Axis, FlyScan, PlanError, Scan, linear


# -- abort interrupts in-flight moves ---------------------------------------
async def test_abort_interrupts_slow_move():
    class Slow(SimSource):
        async def set(self, v):
            await asyncio.sleep(30)           # a 30 s settle / ramp wait
    g, ctl = Slow("g"), Controller()
    t = asyncio.create_task(run(Scan((Axis("g", (0.0, 1.0)),)), {"g": g}, Bus(), ctl))
    await asyncio.sleep(0.05)
    ctl.abort("operator")
    stop = await asyncio.wait_for(t, 2)      # returns promptly, not after 30 s
    assert stop.status == "aborted" and g.safe


async def test_abort_interrupts_fly_ramp_wait():
    m = SimMagnet("B", max_rate=0.01)         # 100 s ramp for 1 T
    ctl = Controller()
    scan = FlyScan("B", 0, 1, rate=0.01, dt=0.5)
    t = asyncio.create_task(run(scan, {"B": m}, Bus(), ctl))
    await asyncio.sleep(0.1)
    ctl.abort()
    assert (await asyncio.wait_for(t, 2)).status == "aborted" and m.safe


# -- a dead sink can't hang or silently lose data ----------------------------
async def test_failed_critical_sink_aborts_run_and_never_blocks():
    bus = Bus()
    n = {"seen": 0}

    async def bad_sink(sub):
        async for _ in sub:
            n["seen"] += 1
            if n["seen"] == 3:
                raise OSError("disk full")
    bus.add_sink("hdf5", bad_sink, maxsize=2, critical=True)   # tiny queue: would block
    g = SimSource("g")
    stop = await asyncio.wait_for(
        run(Scan((Axis("g", linear(0, 1, 500)),)), {"g": g}, bus), 5)
    await bus.join()
    assert stop.status == "aborted"
    assert "sink 'hdf5' failed" in stop.reason and "disk full" in stop.reason
    assert g.safe and bus.failures[0][0] == "hdf5"


async def test_noncritical_sink_failure_does_not_abort():
    bus = Bus()

    async def bad(sub):
        await sub.get()
        raise RuntimeError("plot crashed")
    bus.add_sink("plot", bad, critical=False)
    stop = await asyncio.wait_for(
        run(Scan((Axis("g", linear(0, 1, 50)),)), {"g": SimSource("g")}, bus), 5)
    assert stop.status == "success"


async def test_drop_policy_never_drops_start_stop():
    bus = Bus()
    got = []

    async def slow(sub):
        async for item in sub:
            got.append(item)
            await asyncio.sleep(0.01)
    bus.add_sink("slow", slow, maxsize=2, policy="drop", critical=False)
    await run(Scan((Axis("g", linear(0, 1, 40)),)), {"g": SimSource("g")}, bus)
    await bus.join()
    assert isinstance(got[0], RunStart) and isinstance(got[-1], RunStop)
    assert len([i for i in got if isinstance(i, Event)]) < 40


# -- plan-time data integrity -----------------------------------------------
async def test_duplicate_field_names_rejected_before_move():
    a, b = SimSource("g"), SimSource("g2")
    b.describe = lambda: {"g": a.describe()["g"]}      # same key as device "g"
    with pytest.raises(PlanError, match="produced by both"):
        await run(Scan((Axis("g", (0.0, 1.0)),), readables=("g2",)), {"g": a, "g2": b}, Bus())
    assert a.value == 0.0


# -- per-point timeout feeds the retry policy ---------------------------------
async def test_point_timeout_retried_then_succeeds():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "delay", after=1, times=1, delay_s=5)])
    stop = await run(Scan((Axis("g", linear(0, 1, 3)),), readables=("li",)),
                     {"g": g, "li": li}, Bus(),
                     retry=RetryPolicy(retries=1, point_timeout_s=0.1))
    assert stop.status == "success"


async def test_hung_safe_state_is_bounded():
    class Hung(SimSource):
        async def safe_state(self):
            await asyncio.sleep(60)
    h, ok = Hung("h"), SimSource("ok")
    stop = await asyncio.wait_for(
        run(Scan((Axis("ok", (0.0, 1.0)),)), {"h": h, "ok": ok}, Bus(),
            shutdown_order=["h", "ok"], shutdown_timeout_s=0.1), 5)
    assert ok.safe and stop.status == "failed" and "TimeoutError" in stop.reason


# -- nested fly -------------------------------------------------------------
async def test_fly_nested_under_stepped_axis_snake(tmp_path):
    g, m = SimSource("g"), SimMagnet("B", max_rate=50.0)
    li = SimLockin("li", g)
    scan = FlyScan("B", 0.0, 1.0, rate=50.0, dt=0.004, readables=("li",),
                   outer=(Axis("g", (-1.0, 0.0, 1.0)),), snake=True)
    p = tmp_path / "nested.h5"
    stop = await run_scan(scan, {"g": g, "B": m, "li": li}, out=p, shutdown_order=["g", "B"])
    assert stop.status == "success"
    ds = load(p)
    ramp = ds["ramp"].values
    assert sorted(set(ramp)) == [0, 1, 2]
    assert (ds["seq"].values == np.arange(ds.sizes["sample"])).all()
    for r, gv in zip((0, 1, 2), (-1.0, 0.0, 1.0)):
        sel = ramp == r
        assert (ds["g"].values[sel] == gv).all()
        rb = ds["B_readback"].values[sel]
        if r == 1:                       # snake: middle ramp runs 1 -> 0
            assert rb[0] == pytest.approx(1.0, abs=0.25) and (np.diff(rb) <= 1e-9).all()
        else:
            assert rb[0] == pytest.approx(0.0, abs=0.25) and (np.diff(rb) >= -1e-9).all()
    assert ds.attrs["plan"]["snake"] is True


async def test_fly_outer_limits_validated_before_move():
    g, m = SimSource("g", limits=(-1, 1)), SimMagnet("B")
    scan = FlyScan("B", 0, 1, rate=1, dt=0.1, outer=(Axis("g", (0.0, 5.0)),))
    with pytest.raises(PlanError):
        await run(scan, {"g": g, "B": m}, Bus())


# -- estimate / dry run -----------------------------------------------------
def test_estimate_time_counts_only_moves():
    s = Scan((Axis("a", (0, 1), settle_s=2.0), Axis("b", (0, 1, 2), settle_s=0.5, snake=True)))
    est = s.estimate_time(read_s=0.1)
    # 6 points; a moves at first point of each row (2 s settle dominates b's 0.5 there),
    # b moves on the remaining 4 points (snake: no wasted return move)
    assert est == pytest.approx(6 * 0.1 + 2 * 2.0 + 4 * 0.5)


def test_cli_dry_run(capsys, tmp_path):
    plan = tmp_path / "p.py"
    plan.write_text("from sweepkit.plans import Axis, Scan, linear\n"
                    "SCAN = Scan((Axis('gate', linear(-1, 1, 11), settle_s=0.1),))\n")
    assert cli.main(["dry-run", str(plan), "--station", "station.sim.toml",
                     "--read-s", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "points: 11" in out and "estimated duration" in out
    bad = tmp_path / "bad.py"
    bad.write_text("from sweepkit.plans import Axis, Scan\n"
                   "SCAN = Scan((Axis('bias', (0.0, 5.0)),))\n")     # bias limit is 1
    with pytest.raises(PlanError):
        cli.main(["dry-run", str(bad), "--station", "station.sim.toml"])


# -- api wiring -------------------------------------------------------------
async def test_run_scan_serves_control_and_writes_file(tmp_path):
    g, ctl = SimSource("g", delay=0.01), Controller()
    ports = []
    p = tmp_path / "s.h5"
    task = asyncio.create_task(run_scan(
        Scan((Axis("g", linear(0, 1, 100)),)), {"g": g}, out=p, controller=ctl,
        serve_port=0, on_server_ready=ports.append))
    while not ports:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    st = await send_command("status", port=ports[0])
    assert st["state"] == "running"
    await send_command("abort enough", port=ports[0])
    stop = await asyncio.wait_for(task, 5)
    assert stop.status == "aborted"
    with h5py.File(p) as f:
        assert f.attrs["status"] == "aborted" and f.attrs["reason"] == "enough"


async def test_run_scan_planerror_does_not_hang(tmp_path):
    with pytest.raises(PlanError):
        await asyncio.wait_for(run_scan(
            Scan((Axis("g", (0.0, 99.0)),)), {"g": SimSource("g", limits=(-1, 1))},
            out=tmp_path / "x.h5"), 5)
