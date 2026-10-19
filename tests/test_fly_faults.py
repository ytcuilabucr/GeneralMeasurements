import asyncio

import numpy as np
import pytest

from gmcore.devices.sim import Fault, FaultyDevice, SimLockin, SimMagnet, SimSource
from gmcore.engine import Bus, Controller, Event, RunStop, run
from gmcore import load
from gmcore.plans import Axis, FlyScan, PlanError, Scan, linear
from gmcore.sinks import HDF5Sink


async def _collect(sub):
    return [i async for i in sub]


async def _run_collect(scan, devs, ctl=None, **kw):
    bus = Bus()
    t = asyncio.create_task(_collect(bus.subscribe(policy="block")))
    try:
        stop = await run(scan, devs, bus, ctl, **kw)
    except Exception as e:  # noqa: BLE001
        stop = e
    return stop, await t


def _fly(**kw):
    d = dict(device="B", start=0.0, stop=1.0, rate=20.0, dt=0.005, readables=("li",))
    d.update(kw)
    return FlyScan(**d)


def _devs(**kw):
    m = SimMagnet("B", max_rate=kw.pop("max_rate", 20.0))
    return {"B": m, "li": SimLockin("li", SimSource("v"))}, m


# -- fly -------------------------------------------------------------------
async def test_fly_samples_whole_ramp_monotonic():
    devs, m = _devs()
    stop, items = await _run_collect(_fly(), devs)
    evs = [i for i in items if isinstance(i, Event)]
    assert stop == RunStop("success")
    assert len(evs) >= 5
    rb = [e.data["B"] for e in evs]
    assert all(a <= b + 1e-12 for a, b in zip(rb, rb[1:]))
    assert rb[-1] == pytest.approx(1.0)
    assert [e.seq for e in evs] == list(range(len(evs)))
    assert evs[-1].setpoints["B"] == 1.0 and m.safe


async def test_fly_downward_and_limits_rejected():
    devs, _ = _devs()
    stop, items = await _run_collect(_fly(start=1.0, stop=0.0), devs)
    assert stop.status == "success"
    assert [i for i in items if isinstance(i, Event)][-1].data["B"] == pytest.approx(0.0)
    with pytest.raises(PlanError):
        _fly(stop=50.0).validate(devs)
    with pytest.raises(PlanError):
        _fly(device="li").validate(devs)       # not Rampable


async def test_fly_abort_holds_field():
    devs, m = _devs(max_rate=1.0)
    ctl = Controller()
    scan = _fly(rate=1.0, stop=1.0, dt=0.01)   # 1 s ramp
    task = asyncio.create_task(_run_collect(scan, devs, ctl))
    await asyncio.sleep(0.15)
    ctl.abort("stop it")
    stop, _ = await task
    assert stop.status == "aborted"
    held = m.value
    await asyncio.sleep(0.05)
    assert m.value == held and 0 < held < 1 and m.safe


async def test_fly_pause_holds_then_resumes():
    devs, m = _devs(max_rate=1.0)
    ctl = Controller()
    task = asyncio.create_task(_run_collect(_fly(rate=1.0, dt=0.01), devs, ctl))
    await asyncio.sleep(0.2)
    ctl.pause()
    await asyncio.sleep(0.05)
    v = m.value
    await asyncio.sleep(0.1)
    assert m.value == v and 0 < v < 1
    ctl.resume()
    stop, _items = await asyncio.wait_for(task, 5)
    assert stop.status == "success" and m.value == pytest.approx(1.0)


async def test_fly_timeout_fails_and_holds():
    devs, m = _devs(max_rate=1.0)
    stop, _ = await _run_collect(_fly(rate=1.0, timeout_s=0.1), devs)
    assert isinstance(stop, TimeoutError) and m.safe


async def test_fly_hdf5_streams_and_load(tmp_path):
    devs, _ = _devs()
    p = tmp_path / "fly.h5"
    bus = Bus()
    sink = asyncio.create_task(HDF5Sink(p, flush_every=3).consume(bus.subscribe(policy="block")))
    await run(_fly(), devs, bus)
    await sink
    ds = load(p)
    n = ds.sizes["sample"]
    assert n >= 5 and ds.attrs["status"] == "success" and ds.attrs["mode"] == "fly"
    assert ds["li.x"].dims == ("sample",) and ds["B_readback"].shape == (n,)
    assert ds["B"].values[-1] == 1.0 and (ds["seq"].values == np.arange(n)).all()


# -- fault injection -------------------------------------------------------
async def test_read_timeout_fails_run_but_shuts_down(tmp_path):
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "timeout", after=3)])
    p = tmp_path / "t.h5"
    bus = Bus()
    sink = asyncio.create_task(HDF5Sink(p).consume(bus.subscribe(policy="block")))
    with pytest.raises(TimeoutError):
        await run(Scan((Axis("g", linear(0, 1, 10)),), readables=("li",)),
                  {"g": g, "li": li}, bus)
    await sink
    ds = load(p)
    assert g.safe and ds.attrs["status"] == "failed" and "TimeoutError" in ds.attrs["reason"]
    assert (ds["seq"].values >= 0).sum() == 3


async def test_garbage_reply_stored_as_nan_and_watchdog_can_catch_it():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "garbage", after=2, times=1)])

    async def wd(ev):
        return "nan reading" if np.isnan(ev.data["li.x"]) else None
    stop, items = await _run_collect(
        Scan((Axis("g", linear(0, 1, 6)),), readables=("li",), watchdogs=(wd,)),
        {"g": g, "li": li})
    assert stop.status == "aborted" and "nan reading" in stop.reason
    assert len([i for i in items if isinstance(i, Event)]) == 3 and g.safe


async def test_slow_read_does_not_block_loop():
    g = SimSource("g")
    li = FaultyDevice(SimLockin("li", g), [Fault("read", "delay", times=None, delay_s=0.05)])
    lags = []

    async def ticker():
        while True:
            t = asyncio.get_running_loop().time()
            await asyncio.sleep(0.01)
            lags.append(asyncio.get_running_loop().time() - t - 0.01)
    tk = asyncio.create_task(ticker())
    stop, _ = await _run_collect(Scan((Axis("g", linear(0, 1, 4)),), readables=("li",)),
                                 {"g": g, "li": li})
    tk.cancel()
    assert stop.status == "success" and max(lags) < 0.03


async def test_failing_safe_state_does_not_block_others():
    a, b = SimSource("a"), SimSource("b")
    bad = FaultyDevice(a, [Fault("safe_state", "error", times=None)])
    stop, _ = await _run_collect(Scan((Axis("b", (0.0, 1.0)),)), {"a": bad, "b": b},
                                 shutdown_order=["a", "b"])
    assert b.safe and stop.status == "failed" and "safe_state errors" in stop.reason
