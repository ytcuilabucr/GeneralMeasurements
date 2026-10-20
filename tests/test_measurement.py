"""General Measurement: the measurement model (gmcore/measurement/) and its
window, on simulated instruments."""

import asyncio
import math
import os
import shutil
import time

import pytest

from gmcore.config import load_station
from gmcore.config.registry import build_devices
from gmcore.devices import SR850, FakeTransport, Keithley2400
from gmcore.measurement import MeasurementConfig, StepChannel, StepItem, build, catalog
from gmcore.plans import PlanError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_step_items_give_the_values():
    assert StepItem("single", start=0.5).values() == (0.5,)
    assert StepItem(start=-1, stop=1, n_points=5).values() == (-1, -0.5, 0, 0.5, 1)
    it = StepItem("center_span", start=0, stop=0, step_type="step", step=0.25)
    it.set_center_span(1.0, 1.0)
    assert (it.start, it.stop, it.points()) == (0.5, 1.5, 5)
    assert StepItem(start=1, stop=100, n_points=3, interpolation="log").values() == \
        pytest.approx((1, 10, 100))
    with pytest.raises(ValueError, match="step size"):
        StepItem(step_type="step", step=0).values()
    # items join; the shared end point is kept once
    sc = StepChannel("x", [StepItem(start=0, stop=1, n_points=3),
                           StepItem(start=1, stop=2, n_points=2)])
    assert sc.values() == (0, 0.5, 1, 2) and not sc.single


def test_config_round_trips_through_toml():
    cfg = MeasurementConfig("iv", "a comment", [
        StepChannel("gate.value", [StepItem(start=-1, stop=1, n_points=21),
                                   StepItem("single", start=0)], settle_s=0.1, snake=True)],
        ["lockin.x"], after="safe", data_dir="D")
    text = cfg.to_toml()
    assert text.startswith("# General Measurement")
    assert MeasurementConfig.from_toml(text) == cfg
    with pytest.raises(ValueError, match="after"):
        MeasurementConfig.from_toml('after = "explode"')
    with pytest.raises(ValueError, match="unknown"):
        MeasurementConfig.from_toml('sample = "x"')


async def _devices():
    devs = await build_devices(load_station("tests/fixtures/sim.toml"))
    k = Keithley2400("Keithley 2400 - 1", FakeTransport(lambda c: "0"), "volt", 1e-6,
                     (-1, 1), settle_s=0)
    li = SR850("SR850 - 1", FakeTransport({}))
    devs.update({k.name: k, li.name: li})
    return devs


def test_catalog_from_definitions_and_plain_devices():
    cat = catalog(asyncio.run(_devices()))
    level = cat["Keithley 2400 - 1.level"]
    assert (level.kind, level.unit, level.limits) == ("step", "V", (-1.0, 1.0))
    assert cat["SR850 - 1.frequency"].limits == (0.001, 102000.0)
    assert cat["gate.value"].limits == (-10.0, 10.0)            # a simulator: its set()
    assert cat["Keithley 2400 - 1.i"].kind == "log" and cat["lockin.x"].kind == "log"
    steps = [c.id for c in cat.values() if c.kind == "step"]
    assert "Keithley 2400 - 1.compliance" not in steps          # not writable


def test_build_orders_loops_like_labber_and_checks_limits():
    devs = asyncio.run(_devices())
    cfg = MeasurementConfig(step=[
        StepChannel("gate.value", [StepItem(start=-1, stop=1, n_points=5)]),     # inner
        StepChannel("magnet.value", [StepItem("single", start=0.1)]),            # set once
        StepChannel("bias.value", [StepItem(start=0, stop=0.2, n_points=3)])],   # outer
        log=["lockin.x", "gate"])
    scan, rd = build(cfg, devs)
    assert scan.axis_names == ("magnet.value", "bias.value", "gate.value")
    assert scan.shape == (1, 3, 5) and scan.readables == ("lockin (log)", "gate (log)")
    assert not hasattr(rd["gate"], "safe_state")                 # "hold": no safe state
    cfg.after = "safe"
    assert hasattr(build(cfg, devs)[1]["gate"], "safe_state")
    cfg.step[0].items[0].stop = 20                               # outside gate's ±10
    with pytest.raises(PlanError, match="outside limits"):
        build(cfg, devs)
    with pytest.raises(PlanError, match="not a settable"):
        build(MeasurementConfig(step=[StepChannel("lockin.x")]), devs)
    with pytest.raises(PlanError, match="empty"):
        build(MeasurementConfig(), devs)


def test_a_keithley_with_the_output_off_logs_nan():
    from gmcore.measurement.channels import LogTarget
    k = asyncio.run(_devices())["Keithley 2400 - 1"]
    data = asyncio.run(LogTarget(k.name, k, [f"{k.name}.i"]).read())
    assert math.isnan(data[f"{k.name}.i"])
    assert ":READ?" not in k.t.log                               # never asked


def test_simulated_run_writes_the_grid(tmp_path):
    h5py = pytest.importorskip("h5py")
    from gmcore.api import run_scan

    async def main():
        devs = await _devices()
        cfg = MeasurementConfig(step=[
            StepChannel("gate.value", [StepItem(start=-1, stop=1, n_points=5)]),
            StepChannel("bias.value", [StepItem(start=0, stop=0.2, n_points=3)])],
            log=["lockin.x"])
        scan, rd = build(cfg, devs)
        return await run_scan(scan, rd, out=tmp_path / "m.h5")
    assert asyncio.run(main()).status == "success"
    with h5py.File(tmp_path / "m.h5") as f:
        assert f["data/lockin.x"].shape == (3, 5)
        assert list(f["setpoints/gate.value"]) == [-1, -0.5, 0, 0.5, 1]


def test_data_path_is_filed_by_date_and_never_overwrites(tmp_path):
    pytest.importorskip("PySide6")
    from gmcore.gui.measurement.window import data_path
    t = time.mktime((2026, 10, 19, 12, 0, 0, 0, 0, -1))
    p = data_path(tmp_path, "iv / test", t)
    assert p == tmp_path / "2026" / "10" / "Data_1019" / "iv _ test.h5"
    p.parent.mkdir(parents=True)
    p.touch()
    assert data_path(tmp_path, "iv / test", t).name == "iv _ test_2.h5"


# -- the window --------------------------------------------------------------------------------
@pytest.fixture
def server(tmp_path):
    pytest.importorskip("PySide6QtAds")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets

    from gmcore.gui.common.appsettings import MiscSettings
    from gmcore.gui.server import InstrumentServer
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    shutil.copy("tests/fixtures/sim.toml", tmp_path / "sim.toml")
    s = InstrumentServer(tmp_path / "sim.toml", MiscSettings(tmp_path / "misc.toml"))
    s.connect_all()
    _wait(app, lambda: all(r.state == "connected" for r in s.rows))
    for r in s.rows:
        r.panel.live.setChecked(False)
    yield app, s
    if s.measurement is not None:
        s.measurement._dirty = False
        s.measurement.future = None
        s.measurement.close()
    s._dirty = False
    s.disconnect_all()
    s.hide()
    s.worker.stop()


def _wait(app, cond, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def _yes(monkeypatch):
    from PySide6 import QtWidgets
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)


def _edit(w, text):
    w.setText(text)
    w.editingFinished.emit()


def test_window_builds_and_runs_a_measurement(server, tmp_path, monkeypatch):
    app, s = server
    m = s.open_measurement()
    tree = {m.instruments.topLevelItem(i).text(0): m.instruments.topLevelItem(i)
            for i in range(m.instruments.topLevelItemCount())}
    assert tree["gate"].child(0).text(0) == "Set" and tree["lockin"].child(0).text(0) == "Read"

    m.add_step("gate.value")                    # a single value at first, like Labber
    assert m.cfg.step[0].items[0].range_type == "single"
    m.range_type.setCurrentIndex(m.range_type.findData("start_stop"))
    _edit(m.item_start, "-1")
    _edit(m.item_stop, "1")
    m.n_points.setValue(5)
    assert m.cfg.step[0].values() == (-1, -0.5, 0, 0.5, 1)
    assert m.step_list.topLevelItem(0).text(2) == "5"
    m.add_step("bias.value")                    # outer loop: 3 points, by center and span
    m.range_type.setCurrentIndex(m.range_type.findData("center_span"))
    _edit(m.item_center, "0.1")
    _edit(m.item_span, "0.2")
    m.n_points.setValue(3)
    assert m.cfg.step[1].values() == pytest.approx((0, 0.1, 0.2))
    m.add_log("lockin.x")
    m.add_log("lockin.x")                       # once only
    assert m.cfg.log == ["lockin.x"] and m.estimate.text().startswith("15 points")
    m.move_step(-1)                             # bias first = the inner loop now
    assert [sc.channel for sc in m.cfg.step] == ["bias.value", "gate.value"]
    m.move_step(1)

    m._meta_changed(name="iv test", data_dir=str(tmp_path / "Data"))
    _yes(monkeypatch)
    assert m.start()
    assert m.running and not m.lists.isEnabled() and m.btn_abort.isEnabled()
    _wait(app, lambda: not m.running)
    assert m.buffer.n == 15 and m.progress.value() == 15
    assert m.run_path.name == "iv test.h5" and m.run_path.exists()
    assert m.run_path.parent.name.startswith("Data_")
    assert "success" in s.log_view.toPlainText() and m.lists.isEnabled()
    x, _y = m.buffer.series("lockin.x")
    assert len(x) == 5 and m.plot_y.currentData() == "lockin.x"

    path = tmp_path / "iv.toml"                 # save, then open it again
    assert m._write(path)
    m.cfg = MeasurementConfig()
    assert m.load(path) and [sc.channel for sc in m.cfg.step] == ["gate.value", "bias.value"]


def test_window_pause_and_stop(server, tmp_path, monkeypatch):
    app, s = server
    m = s.open_measurement()
    m.add_step("magnet.value")                  # the simulated magnet ramps: slow
    m.range_type.setCurrentIndex(m.range_type.findData("start_stop"))
    _edit(m.item_stop, "5")
    m.n_points.setValue(50)
    m._meta_changed(data_dir=str(tmp_path))
    _yes(monkeypatch)
    assert m.start()
    m.btn_pause.setChecked(True)
    assert m.btn_pause.text() == "Resume"
    m.stop()
    _wait(app, lambda: not m.running)
    assert "aborted" in s.log_view.toPlainText() and not m.btn_pause.isChecked()


def test_a_disconnected_channel_is_marked_and_refused(server):
    app, s = server
    m = s.open_measurement()
    m.add_step("gate.value")
    s.disconnect(s.row("gate"))
    _wait(app, lambda: "gate.value" not in m.channels)
    assert "not connected" in m.step_list.topLevelItem(0).text(0)
    assert "not a settable channel" in m.estimate.text()


def test_window_time_trace_samples_the_register(server, tmp_path, monkeypatch):
    """Timer inner loop, a gate value in the outer loop; the lock-in's panel polls
    at 0.2 s, the Timer steps 0.05 s: auto samples the server's register."""
    app, s = server
    lockin = s.row("lockin").panel
    lockin.interval.setValue(0.2)
    lockin.live.setChecked(True)
    _wait(app, lambda: lockin.read_time_s() is not None)        # the register fills
    reads = []
    dev = s.devices["lockin"]
    real = dev.read

    async def counted():
        reads.append(1)
        return await real()
    dev.read = counted

    m = s.open_measurement()
    m.add_step("Timer.timer")
    m.range_type.setCurrentIndex(m.range_type.findData("start_stop"))
    _edit(m.item_start, "0")
    _edit(m.item_stop, "0.5")
    m.n_points.setValue(11)
    m.add_step("gate.value")                     # the outer value, set once per pass
    m.range_type.setCurrentIndex(m.range_type.findData("start_stop"))
    _edit(m.item_start, "0")
    _edit(m.item_stop, "1")
    m.n_points.setValue(2)
    m.add_log("Timer.clock")
    m.add_log("lockin.x")
    assert "lockin: server register (panel reads every 0.2 s)" in m.log_source_info.text()
    assert "about 1 s" in m.estimate.text()      # two passes of 0.5 s
    m._meta_changed(name="trace", data_dir=str(tmp_path))
    _yes(monkeypatch)
    polls_before = len(reads)
    assert m.start()
    _wait(app, lambda: not m.running)
    assert m.buffer.n == 22
    # only the panel read the lock-in: about one poll per 0.2 s of the ~1 s run
    assert len(reads) - polls_before <= 8
    h5py = pytest.importorskip("h5py")
    with h5py.File(m.run_path) as f:
        clock = f["data/Timer.clock"][...]
        age = f["data/lockin.sample_age"][...]
    assert clock.shape == (2, 11) and abs(clock[0, 10] - 0.5) < 0.03
    assert (age < 0.5).all()                     # sampled values at most a poll or two old
    m.log_source.setCurrentIndex(m.log_source.findData("direct"))
    assert "lockin: read directly" in m.log_source_info.text()
