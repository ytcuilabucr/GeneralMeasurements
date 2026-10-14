import asyncio

import pytest

from sweepkit.devices.sim import SimLockin, SimSource
from sweepkit.engine import Bus, Controller, Event, RunStart, RunStop, run
from sweepkit.plans import Axis, PlanError, Scan, linear


def test_snake_2d_path_is_continuous():
    s = Scan((Axis("a", (0, 1, 2)), Axis("b", (0, 1, 2), snake=True)))
    idx = [i for _, i, _ in s.points()]
    assert idx[:6] == [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]
    assert s.shape == (3, 3) and len(idx) == 9


def test_snake_3d_continuous():
    s = Scan((Axis("a", (0, 1)), Axis("b", (0, 1, 2), snake=True),
              Axis("c", (0, 1, 2), snake=True)))
    idx = [i for _, i, _ in s.points()]
    assert len(idx) == 18
    for p, q in zip(idx, idx[1:]):
        assert sum(abs(x - y) for x, y in zip(p, q)) == 1


def test_out_of_limits_rejected_before_move():
    src = SimSource("g", limits=(-1, 1))
    with pytest.raises(PlanError):
        Scan((Axis("g", linear(0, 5, 3)),)).validate({"g": src})


async def _collect(sub):
    return [i async for i in sub]


async def test_2d_snake_scan_end_to_end():
    g, b = SimSource("g"), SimSource("b")
    li = SimLockin("li", g)
    scan = Scan((Axis("b", linear(0, 1, 2)), Axis("g", linear(-1, 1, 3), snake=True)),
                readables=("li",))
    bus = Bus()
    task = asyncio.create_task(_collect(bus.subscribe(policy="block")))
    stop = await run(scan, {"g": g, "b": b, "li": li}, bus)
    items = await task
    assert stop == RunStop("success") and isinstance(items[0], RunStart)
    evs = [i for i in items if isinstance(i, Event)]
    assert len(evs) == 6
    assert evs[3].setpoints == {"b": 1.0, "g": 1.0}       # snake reversed on row 2
    assert evs[0].data["g"] == -1.0 and "li.x" in evs[0].data  # readback recorded
    assert g.safe and b.safe


async def test_only_changed_axes_move():
    class Counting(SimSource):
        n = 0

        async def set(self, v):
            self.n += 1
            await super().set(v)
    a, b = Counting("a"), Counting("b")
    await run(Scan((Axis("a", (0, 1)), Axis("b", (0, 1, 2)))), {"a": a, "b": b}, Bus())
    assert a.n == 2 and b.n == 6


async def test_watchdog_abort_runs_safe_state():
    g = SimSource("g")

    async def wd(ev):
        return "tripped" if ev.seq == 4 else None
    scan = Scan((Axis("g", linear(0, 1, 50)),), watchdogs=(wd,))
    stop = await run(scan, {"g": g}, Bus())
    assert stop.status == "aborted" and "tripped" in stop.reason
    assert g.safe and g.value == 0.0


async def test_pause_resume_and_user_abort():
    g = SimSource("g", delay=0.01)
    ctl = Controller()
    bus = Bus()
    sub = bus.subscribe(policy="block")
    task = asyncio.create_task(run(Scan((Axis("g", linear(0, 1, 100)),)), {"g": g}, bus, ctl))
    await sub.get()                    # RunStart
    await sub.get()                    # first event
    ctl.pause()
    await asyncio.sleep(0.05)
    n = sub.queue.qsize()
    await asyncio.sleep(0.1)
    assert sub.queue.qsize() <= n      # held while paused
    ctl.resume()
    await sub.get()
    ctl.abort("done")
    assert (await task).status == "aborted"
    assert g.safe


async def test_failure_still_shuts_down_and_reraises():
    class Bad(SimSource):
        async def read(self):
            raise RuntimeError("boom")
    g = SimSource("g")
    bus = Bus()
    sub = bus.subscribe(policy="block")
    with pytest.raises(RuntimeError):
        await run(Scan((Axis("g", (0, 1)),), readables=("bad",)),
                  {"g": g, "bad": Bad("bad")}, bus)
    items = await _collect(sub)
    assert items[-1].status == "failed" and g.safe
