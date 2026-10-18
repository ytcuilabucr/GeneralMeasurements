import asyncio

import numpy as np
import pytest

from generalmeasurements import cli
from generalmeasurements.devices import FakeTransport, Keithley24xx
from generalmeasurements.devices.sim import Fault, FaultyDevice, SimLockin, SimSource
from generalmeasurements.engine import (Bus, Controller, Event, LoopLagMonitor, Note, RetryPolicy,
                             compliance_watchdog, nan_watchdog, range_watchdog, run)
from generalmeasurements.engine.remote import ControlServer, StatusTracker, send_command
from generalmeasurements import load
from generalmeasurements.plans import Axis, Scan, linear
from generalmeasurements.sinks import HDF5Sink, NotifySink


async def _collect(sub):
    return [i async for i in sub]


async def _go(scan, devs, ctl=None, **kw):
    bus = Bus()
    t = asyncio.create_task(_collect(bus.subscribe(policy="block")))
    stop = await run(scan, devs, bus, ctl, **kw)
    return stop, await t


# -- compliance trip on a (fake-transport) Keithley -----------------------
async def test_compliance_trip_aborts_and_ramps_gate_to_zero():
    reads = {"n": 0}

    def reply(cmd: str) -> str:
        if cmd == ":READ?":
            reads["n"] += 1
            return "1.0,1e-9,0,0,0"
        if cmd == ":SENS:CURR:PROT:TRIP?":
            return "1" if reads["n"] >= 4 else "0"
        if cmd == "*IDN?":
            return "KEITHLEY,MODEL 2400"
        raise AssertionError(cmd)
    t = FakeTransport(reply)
    gate = Keithley24xx("gate", t, "2400", limits=(-5, 5), max_rate=1000,
                        step_dt=0.0005, settle_s=0, compliance=1e-9)
    scan = Scan((Axis("gate", linear(0, 3, 20)),),
                watchdogs=(compliance_watchdog("gate.compliance"),))
    stop, items = await _go(scan, {"gate": gate})
    assert stop.status == "aborted" and "gate.compliance" in stop.reason
    assert len([i for i in items if isinstance(i, Event)]) == 4
    assert any(isinstance(i, Note) and "watchdog" in i.message for i in items)
    lev = [c for c in t.log if c.startswith(":SOUR:VOLT:LEV")]
    assert lev[-1] == ":SOUR:VOLT:LEV 0" and t.log[-1] == ":OUTP OFF"


async def test_rate_limit_enforced_inside_set():
    t = FakeTransport()
    k = Keithley24xx("g", t, "2400", limits=(-5, 5), max_rate=1.0, step_dt=0.01, settle_s=0)
    await k.set(0.05)
    levels = [float(c.split()[-1]) for c in t.log if c.startswith(":SOUR:VOLT:LEV")]
    steps = np.diff([0.0] + levels)
    assert (steps <= 0.01 + 1e-9).all() and levels[-1] == 0.05


# -- watchdog factories ----------------------------------------------------
async def test_range_and_nan_watchdogs():
    ev = lambda **d: Event(7, (7,), {}, d)  # noqa: E731
    assert await range_watchdog("i", -1, 1)(ev(i=0.5)) is None
    assert "outside" in await range_watchdog("i", -1, 1)(ev(i=2.0))
    wd = nan_watchdog("x", max_consecutive=2)
    assert await wd(ev(x=float("nan"))) is None
    assert "non-finite" in await wd(ev(x=float("nan")))
    assert await nan_watchdog("x")(ev(x=1.0)) is None


# -- retry policy ----------------------------------------------------------
async def test_retry_recovers_from_transient_timeout():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "timeout", after=2, times=2)])
    stop, items = await _go(Scan((Axis("g", linear(0, 1, 5)),), readables=("li",)),
                            {"g": g, "li": li}, retry=RetryPolicy(retries=3))
    assert stop.status == "success"
    assert len([i for i in items if isinstance(i, Event)]) == 5
    assert len([i for i in items if isinstance(i, Note)]) == 2


async def test_retry_exhausted_aborts_by_default():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "timeout", after=1, times=None)])
    with pytest.raises(TimeoutError):
        await _go(Scan((Axis("g", linear(0, 1, 5)),), readables=("li",)),
                  {"g": g, "li": li}, retry=RetryPolicy(retries=1))
    assert g.safe


async def test_retry_exhausted_pauses_then_operator_resumes():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "timeout", after=1, times=2)])
    ctl = Controller()
    bus = Bus()
    sub = bus.subscribe(policy="block")
    task = asyncio.create_task(run(Scan((Axis("g", linear(0, 1, 4)),), readables=("li",)),
                                   {"g": g, "li": li}, bus, ctl,
                                   retry=RetryPolicy(retries=1, on_exhausted="pause")))
    seen = []
    while not ctl.paused:
        seen.append(await asyncio.wait_for(sub.get(), 2))
    assert any(isinstance(i, Note) and "paused" in i.message for i in seen)
    await asyncio.sleep(0.05)
    assert not task.done()                     # holding for the operator
    ctl.resume()
    stop = await asyncio.wait_for(task, 5)
    assert stop.status == "success"


# -- pause holds state without touching the instrument ---------------------
async def test_pause_issues_no_instrument_commands():
    t = FakeTransport({":READ?": "0,0,0,0,0", ":SENS:CURR:PROT:TRIP?": "0",
                       "*IDN?": "KEITHLEY,MODEL 2400"})
    k = Keithley24xx("g", t, "2400", limits=(-5, 5), max_rate=1000, step_dt=0.0005,
                     settle_s=0.01)
    ctl = Controller()
    bus = Bus()
    sub = bus.subscribe(policy="block")
    task = asyncio.create_task(run(Scan((Axis("g", linear(0, 1, 30)),)), {"g": k}, bus, ctl))
    await sub.get()
    await sub.get()
    ctl.pause()
    await asyncio.sleep(0.1)                   # let the in-flight point finish
    n = len(t.log)
    await asyncio.sleep(0.3)
    assert len(t.log) == n                     # held: nothing sent while paused
    ctl.resume()
    assert (await asyncio.wait_for(task, 5)).status == "success"


# -- remote control --------------------------------------------------------
async def test_control_server_pause_resume_abort_status():
    ctl = Controller()
    tracker = StatusTracker(ctl)
    srv = ControlServer(ctl, tracker, port=0)
    await srv.start()
    try:
        bus = Bus()
        tt = asyncio.create_task(tracker.consume(bus.subscribe(policy="block")))
        g = SimSource("g", delay=0.01)
        rt = asyncio.create_task(run(Scan((Axis("g", linear(0, 1, 200)),)), {"g": g}, bus, ctl))
        await asyncio.sleep(0.1)
        r = await send_command("status", port=srv.port)
        assert r["ok"] and r["state"] == "running" and r["total"] == 200
        assert (await send_command("pause", port=srv.port))["state"] == "paused"
        assert ctl.paused
        await send_command("resume", port=srv.port)
        assert (await send_command("bogus", port=srv.port))["ok"] is False
        await send_command("abort too hot", port=srv.port)
        stop = await asyncio.wait_for(rt, 5)
        await tt
        assert stop.status == "aborted" and stop.reason == "too hot"
        assert tracker.snapshot()["state"] == "aborted"
    finally:
        await srv.stop()


# -- notifier --------------------------------------------------------------
async def test_notify_sink_reports_failure_and_survives_send_errors():
    sent = []

    async def send(text):
        sent.append(text)
        if len(sent) == 1:
            raise OSError("network down")      # must not disturb the run

    bus = Bus()
    nt = asyncio.create_task(NotifySink(send, "cryo", notify_success=False)
                             .consume(bus.subscribe(policy="block")))

    async def wd(ev):
        return "hot" if ev.seq == 1 else None
    g = SimSource("g")
    stop = await run(Scan((Axis("g", (0.0, 1.0, 2.0)),), watchdogs=(wd,)), {"g": g}, bus)
    await nt
    assert stop.status == "aborted"
    assert any("aborted" in s and "hot" in s for s in sent) and all(s.startswith("[cryo]") for s in sent)


async def test_hdf5_log_records_notes(tmp_path):
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "timeout", after=1, times=1)])
    p = tmp_path / "n.h5"
    bus = Bus()
    sink = asyncio.create_task(HDF5Sink(p).consume(bus.subscribe(policy="block")))
    await run(Scan((Axis("g", linear(0, 1, 3)),), readables=("li",)), {"g": g, "li": li}, bus,
              retry=RetryPolicy(retries=1))
    await sink
    assert any("retry 1/1" in s for s in load(p).attrs["log"])


async def test_loop_lag_monitor_detects_blocking():
    import time
    m = LoopLagMonitor(threshold_s=0.05, interval_s=0.01)
    m.start()
    await asyncio.sleep(0.03)
    time.sleep(0.12)                           # a blocking driver call
    await asyncio.sleep(0.03)
    await m.stop()
    assert m.stalls and max(m.stalls) > 0.05


# -- CLI end to end on the simulated station -------------------------------
def test_cli_validate_and_run_sim(tmp_path, capsys):
    assert cli.main(["validate", "station.sim.toml"]) == 0
    plan = tmp_path / "plan.py"
    plan.write_text(
        "from generalmeasurements.plans import Axis, Scan, linear\n"
        "SCAN = Scan((Axis('gate', linear(-1, 1, 5)),), readables=('lockin',))\n")
    rc = cli.main(["run", str(plan), "--station", "station.sim.toml", "--out", str(tmp_path),
                   "--sample", "T1", "--port", "0"])
    assert rc == 0
    files = list(tmp_path.glob("*_T1.h5"))
    assert len(files) == 1
    ds = load(files[0])
    assert ds.attrs["status"] == "success" and ds.attrs["provenance"]["sample"] == "T1"
    assert ds.sizes["gate"] == 5


async def test_prepare_turns_output_on_only_for_devices_in_scan():
    idn = {"*IDN?": "KEITHLEY,MODEL 2400", ":READ?": "0,0,0,0,0",
           ":SENS:CURR:PROT:TRIP?": "0"}
    ta, tb = FakeTransport(dict(idn)), FakeTransport(dict(idn))
    kw = dict(limits=(-5, 5), max_rate=1000, step_dt=0.0005, settle_s=0)
    a, b = Keithley24xx("a", ta, "2400", **kw), Keithley24xx("b", tb, "2400", **kw)
    stop, _ = await _go(Scan((Axis("a", (0.0, 1.0)),)), {"a": a, "b": b})
    assert stop.status == "success"
    assert ":OUTP ON" in ta.log and ta.log.index(":OUTP ON") < ta.log.index(":SOUR:VOLT:LEV 1")
    assert ta.log[-1] == ":OUTP OFF"
    assert ":OUTP ON" not in tb.log        # unused device untouched by prepare
