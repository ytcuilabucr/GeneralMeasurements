import asyncio

import h5py
import numpy as np
import pytest

from sweepkit.devices.sim import SimLockin, SimSource
from sweepkit.engine import Bus, run
from sweepkit.load import load
from sweepkit.plans import Axis, PlanError, Scan, linear
from sweepkit.provenance import collect
from sweepkit.sinks import HDF5Sink, resume_point


def _station():
    g, b = SimSource("g"), SimSource("b")
    return {"g": g, "b": b, "li": SimLockin("li", g)}


def _scan(**kw):
    return Scan((Axis("b", linear(0, 1, 3)), Axis("g", linear(-1, 1, 4), snake=True)),
                readables=("li",), metadata={"sample": "S1"}, **kw)


async def _run(path, scan, devices, resume=False, start_seq=0):
    bus = Bus()
    sink = HDF5Sink(path, flush_every=2, resume=resume)
    t = asyncio.create_task(sink.consume(bus.subscribe(policy="block")))
    stop = await run(scan, devices, bus, provenance=collect(), start_seq=start_seq)
    await t
    return stop


async def test_file_layout_and_load(tmp_path):
    p = tmp_path / "r.h5"
    stop = await _run(p, _scan(), _station())
    assert stop.status == "success"
    with h5py.File(p) as f:
        assert f.attrs["status"] == "success" and f.attrs["schema_version"] == 1
        assert f["data/li.x"].shape == (3, 4)
        assert f["data/li.x"].attrs["units"] == "V"
        assert (f["data/seq"][...] >= 0).all()
        assert f["provenance"].attrs["sweepkit_version"]
    ds = load(p)
    assert ds["li.x"].dims == ("b", "g") and ds.sizes == {"b": 3, "g": 4}
    assert ds["g_readback"].shape == (3, 4)          # axis readback kept
    np.testing.assert_allclose(ds["g_readback"].values[0], ds["g"].values)
    assert ds.attrs["status"] == "success" and ds.attrs["plan"]["metadata"] == {"sample": "S1"}


async def test_abort_leaves_readable_file_with_status(tmp_path):
    p = tmp_path / "a.h5"

    async def wd(ev):
        return "trip" if ev.seq == 5 else None
    stop = await _run(p, _scan(watchdogs=(wd,)), _station())
    assert stop.status == "aborted"
    ds = load(p)
    assert ds.attrs["status"] == "aborted" and "trip" in ds.attrs["reason"]
    assert (ds["seq"].values >= 0).sum() == 6 and np.isnan(ds["li.x"].values).sum() == 6


async def test_resume_finishes_interrupted_run(tmp_path):
    p = tmp_path / "x.h5"
    async def wd(ev):
        return "crash" if ev.seq == 4 else None
    await _run(p, _scan(watchdogs=(wd,)), _station())
    n = resume_point(p)
    assert n == 5
    # simulate a hard kill: status left at "running"
    with h5py.File(p, "r+") as f:
        f.attrs["status"] = "running"
    stop = await _run(p, _scan(), _station(), resume=True, start_seq=n)
    assert stop.status == "success"
    ds = load(p)
    assert (ds["seq"].values >= 0).all() and not np.isnan(ds["li.x"].values).any()
    assert any("resumed" in s for s in ds.attrs["log"])


async def test_resume_rejects_changed_plan(tmp_path):
    p = tmp_path / "y.h5"
    await _run(p, _scan(), _station())
    other = Scan((Axis("b", linear(0, 2, 3)), Axis("g", linear(-1, 1, 4))), readables=("li",))
    with pytest.raises(ValueError):
        await _run(p, other, _station(), resume=True)
