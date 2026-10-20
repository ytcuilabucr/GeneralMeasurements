"""The Timer (gmcore/measurement/timer.py, after Labber's Timer driver) and the
server's register (register.py): exact-interval time traces, and sampling the
panels' readings instead of reading the hardware."""

import asyncio
import math

import pytest

from gmcore.config import load_station
from gmcore.config.registry import build_devices
from gmcore.measurement import (MeasurementConfig, Register, StepChannel, StepItem, Timer,
                                build, catalog, estimate_s, log_sources, timer_step)
from gmcore.plans import PlanError


def _trace(stop=0.5, n=11):
    return StepChannel("Timer.timer", [StepItem(start=0, stop=stop, n_points=n)])


# -- the Timer -----------------------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_timer_waits_to_absolute_times_and_resets_like_labber(monkeypatch):
    clock = FakeClock()

    async def fake_sleep(s):                         # time passes only while waiting
        clock.t += max(s, 1e-4)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    tmr = Timer(clock)

    async def go():
        await tmr.set(0.0)                           # reset: clock reads 0
        assert tmr.elapsed() == pytest.approx(0.0)
        await tmr.set(1.0)                           # waits until 1 s since the reset
        assert tmr.elapsed() == pytest.approx(1.0, abs=1e-3)
        clock.t += 0.3                               # a slow read: 0.3 s
        await tmr.set(2.0)                           # still due at 2 s, not 2.3 s
        assert tmr.elapsed() == pytest.approx(2.0, abs=1e-3)
        clock.t += 1.5                               # a read longer than the step
        await tmr.set(3.0)                           # already due: at once, counted late
        assert tmr.elapsed() == pytest.approx(3.5) and tmr.late == 1
        await tmr.set(0.5)                           # lower than the last: a new pass
        assert tmr.elapsed() == pytest.approx(0.5)
    asyncio.run(go())
    with pytest.raises(ValueError, match="counts up"):
        asyncio.run(tmr.set(-1))


async def _sim():
    return await build_devices(load_station("tests/fixtures/sim.toml"))


def test_a_time_trace_keeps_to_the_grid_with_slow_reads():
    """Through the real runner: 0.05 s steps, a 30 ms read; the clock stays on the
    grid (no drift), and every outer value starts a new pass from 0."""
    from gmcore.api import run_scan

    async def main():
        devs = await _sim()
        lockin = devs["lockin"]
        read = lockin.read

        async def slow_read():
            await asyncio.sleep(0.03)
            return await read()
        lockin.read = slow_read
        cfg = MeasurementConfig(step=[
            _trace(0.5, 11),
            StepChannel("gate.value", [StepItem(start=0, stop=1, n_points=2)])],
            log=["Timer.clock", "lockin.x"])
        scan, rd = build(cfg, devs)
        clocks = []

        async def collect(sub):
            async for item in sub:
                if hasattr(item, "data") and "Timer.clock" in getattr(item, "data", {}):
                    clocks.append(item.data["Timer.clock"])
        stop = await run_scan(scan, rd, sinks=[("c", collect)])
        return stop, clocks
    stop, clocks = asyncio.run(main())
    assert stop.status == "success" and len(clocks) == 22
    grid = [0.05 * i for i in range(11)] * 2
    errors = [c - g for c, g in zip(clocks, grid)]
    assert all(e >= -0.002 for e in errors)          # never early
    assert max(errors) < 0.03                        # read lag + scheduling, never cumulative
    assert errors[10] < 0.03                         # the last point of a pass: no drift


def test_the_timer_starts_after_the_outer_channel_is_in_place():
    """The Timer is stage 1: an outer channel's set and its wait come first, so a
    pass's clock starts once the value is set."""
    async def main():
        devs = await _sim()
        cfg = MeasurementConfig(step=[
            _trace(0.1, 3),
            StepChannel("gate.value", [StepItem(start=0, stop=1, n_points=2)], settle_s=0.2)],
            log=["Timer.clock"])
        scan, rd = build(cfg, devs)
        assert [a.stage for a in scan.axes] == [0, 1]   # gate outer, Timer inner
        from gmcore.api import run_scan
        clocks = []

        async def collect(sub):
            async for item in sub:
                if "Timer.clock" in getattr(item, "data", {}):
                    clocks.append(item.data["Timer.clock"])
        await run_scan(scan, rd, sinks=[("c", collect)])
        return clocks
    clocks = asyncio.run(main())
    assert clocks[0] < 0.02 and clocks[3] < 0.02         # each pass starts at 0, after the wait


def test_estimate_counts_the_timer():
    async def main():
        devs = await _sim()
        cfg = MeasurementConfig(step=[
            _trace(2.0, 21),
            StepChannel("gate.value", [StepItem(start=0, stop=1, n_points=3)], settle_s=1.0)],
            log=["lockin.x"])
        return build(cfg, devs)[0]
    scan = asyncio.run(main())
    # 3 passes of 2 s, each after the gate's 1 s wait, + 63 reads of 0.01 s
    assert estimate_s(scan, read_s=0.01) == pytest.approx(3 * (1.0 + 2.0) + 0.01, abs=0.05)


# -- where readings come from --------------------------------------------------------------
def test_auto_samples_the_register_when_the_intervals_differ():
    cat = asyncio.run(_sim())
    cat = catalog(cat)
    cfg = MeasurementConfig(step=[_trace(1.0, 11)], log=["lockin.x", "gate", "Timer.clock"])
    assert timer_step(cfg) == pytest.approx(0.1)
    # lockin polled every 0.5 s: another interval -> register; gate polled at 0.1 s -> direct
    assert log_sources(cfg, cat, {"lockin": 0.5, "gate": 0.1}) == \
        {"lockin": "register", "gate": "direct"}
    # a panel that isn't polling: read directly (nothing else reads it)
    assert log_sources(cfg, cat, {"lockin": None, "gate": None}) == \
        {"lockin": "direct", "gate": "direct"}
    # no Timer: Labber's behaviour, read at every point
    cfg2 = MeasurementConfig(step=[StepChannel("gate.value")], log=["lockin.x"])
    assert log_sources(cfg2, cat, {"lockin": 0.5}) == {"lockin": "direct"}
    cfg.log_source = "register"
    assert log_sources(cfg, cat, {"lockin": 0.1})["lockin"] == "register"


def test_sampled_instruments_are_never_read_by_the_measurement():
    reg = Register()

    async def main():
        devs = await _sim()
        lockin = devs["lockin"]

        async def no_read():
            raise AssertionError("the measurement read the sampled instrument")
        lockin.read = no_read
        cfg = MeasurementConfig(step=[_trace(0.2, 3)], log=["lockin.x", "lockin.y"])
        forced = MeasurementConfig(step=[_trace(0.2, 3)], log=["lockin.x"],
                                   log_source="register")
        with pytest.raises(PlanError, match="Live is off"):    # nothing fills the register
            build(forced, devs, reg, {"lockin": None})
        scan, rd = build(cfg, devs, reg, {"lockin": 0.5})
        assert scan.metadata["log_sources"] == {"lockin": "register"}
        reg.update("lockin", reg._now() - 0.2, {"lockin.x": 1.5, "lockin.y": -2.0,
                                                "_ramping": True})
        target = rd["lockin (log)"]
        data = await target.read()
        assert set(target.describe()) == {"lockin.x", "lockin.y", "lockin.sample_age"}
        return data
    data = asyncio.run(main())
    assert (data["lockin.x"], data["lockin.y"]) == (1.5, -2.0)
    assert data["lockin.sample_age"] == pytest.approx(0.2, abs=0.05)
    reg.drop("lockin")                               # disconnected: no current values
    values, age = reg.sample("lockin", ["lockin.x"])
    assert math.isnan(values["lockin.x"]) and math.isnan(age)


def test_config_keeps_the_log_source():
    cfg = MeasurementConfig(step=[_trace()], log=["Timer.clock"], log_source="register")
    assert MeasurementConfig.from_toml(cfg.to_toml()) == cfg
    with pytest.raises(ValueError, match="log_source"):
        MeasurementConfig.from_toml('log_source = "psychic"')


def test_an_instrument_named_timer_is_refused():
    devs = asyncio.run(_sim())
    devs["Timer"] = devs["gate"]
    with pytest.raises(PlanError, match="built-in timer"):
        build(MeasurementConfig(step=[StepChannel("gate.value")]), devs)
