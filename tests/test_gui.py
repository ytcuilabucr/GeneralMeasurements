"""GUI on simulators and fake transports, offscreen (no display, no hardware)."""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ads = pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
from PySide6 import QtCore, QtWidgets  # noqa: E402

from generalmeasurements.devices import (FakeTransport, Gigatronics2420B,  # noqa: E402
                                         Keithley24xx, LakeShore625)
from generalmeasurements.gui import panels  # noqa: E402
from generalmeasurements.gui.app import StationWindow  # noqa: E402
from generalmeasurements.gui.panels import fmt, parse_si  # noqa: E402

YES = QtWidgets.QMessageBox.StandardButton.Yes


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    wins = []

    def make(station, ini="gui.ini"):
        w = StationWindow(station, QtCore.QSettings(str(tmp_path / ini),
                                                    QtCore.QSettings.Format.IniFormat))
        wins.append(w)
        return w
    yield make
    for w in wins:
        for p in w.panels.values():
            p.timer.stop()
        for iw in [*w.windows.values(), *w.views.values()]:
            iw.hide()
        w.hide()
        w.worker.stop()


def wait(app, cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return
        time.sleep(0.005)
    logs = [w.log_view.toPlainText()[-800:] for w in QtWidgets.QApplication.topLevelWidgets()
            if isinstance(w, StationWindow)]
    raise AssertionError("condition not met in time; station log:\n" + "\n".join(logs))


def test_fmt_and_parse():
    assert fmt(1.05e-6, "A") == "1.05 µA"
    assert fmt(7.054e9, "Hz") == "7.054 GHz"
    assert fmt(9.9e37, "Ohm") == "overrange"
    assert fmt(77.589, "K") == "77.589 K"
    assert parse_si("100n") == pytest.approx(1e-7)
    assert parse_si("7.054G") == pytest.approx(7.054e9)
    assert parse_si("-1.5 mV") == pytest.approx(-1.5e-3)
    assert parse_si("1e-7") == pytest.approx(1e-7)
    with pytest.raises(ValueError):
        parse_si("abc")


def test_lab_station_one_window_per_instrument_type(app, window):
    w = window("station.lab.toml")
    groups = {t: [p.name for p in iw.panels] for t, iw in w.windows.items()}
    assert groups == {"Keithley": ["gate", "bias", "dmm"], "Lock-in": ["lockin"],
                      "Temperature": ["temp"], "Magnet": ["magnet"], "RF source": ["rf"],
                      "Helium level": ["helium"]}
    k = w.windows["Keithley"]
    assert all(k.dock(n).isTabbed() for n in ("gate", "bias", "dmm"))   # one tabbed area
    assert k.dock("gate").dockAreaWidget() is k.dock("dmm").dockAreaWidget()


def test_window_key_overrides_grouping(app, window, tmp_path):
    src = open("station.sim.toml", encoding="utf-8").read()
    src = src.replace('driver = "sim_magnet"', 'driver = "sim_magnet"\nwindow = "Sources"')
    (tmp_path / "st.toml").write_text(src, "utf-8")
    w = window(tmp_path / "st.toml")
    assert [p.name for p in w.windows["Sources"].panels] == ["gate", "bias", "magnet"]
    assert "Magnet" not in w.windows


def test_layout_saved_and_restored(app, window):
    w = window("station.lab.toml")
    w.show()
    k = w.windows["Keithley"]
    area = k.dock("gate").dockAreaWidget()
    k.docks.addDockWidget(ads.RightDockWidgetArea, k.dock("dmm"), area)
    assert not k.dock("dmm").isTabbed()                 # split out beside the others
    w.windows["Magnet"].close()
    w.save_layout()
    w2 = window("station.lab.toml")                     # same ini file
    k2 = w2.windows["Keithley"]
    assert not k2.dock("dmm").isTabbed() and k2.dock("gate").isTabbed()
    w2.show()
    assert not w2.windows["Magnet"].isVisible() and k2.isVisible()
    w2.reset_layout()
    assert k2.dock("dmm").isTabbed()


def test_callbacks_run_on_the_gui_thread(app, window):
    w = window("station.sim.toml")
    seen = []

    async def where():
        return threading.get_ident()
    w.worker.submit(where(), lambda r: seen.append((r, threading.get_ident())))
    wait(app, lambda: seen)
    loop_thread, callback_thread = seen[0]
    assert loop_thread != callback_thread == threading.get_ident()


def test_sim_station_connect_set_and_ramp(app, window, monkeypatch):
    w = window("station.sim.toml")
    assert set(w.panels) == {"gate", "bias", "magnet", "lockin"}
    w.connect_all()
    wait(app, lambda: len(w.devices) == 4)            # lockin waits for its source
    wait(app, lambda: all(p.conn_state == "connected" for p in w.panels.values()))
    gate = w.panels["gate"]
    gate.target.setText("1.5")
    gate._go()
    wait(app, lambda: w.devices["gate"].value == 1.5)
    gate.poll()
    wait(app, lambda: gate._labels["gate"][0].text() == "1.5 V")
    mag = w.panels["magnet"]
    mag.target.setText("0.5")
    mag.rate.setText("5")
    mag._go()
    wait(app, lambda: w.devices["magnet"].value == pytest.approx(0.5))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: YES)
    w.windows["Magnet"].all_safe()                    # this window's instruments only
    wait(app, lambda: w.devices["magnet"].safe)
    assert not w.devices["gate"].safe
    w.all_safe()
    wait(app, lambda: w.devices["gate"].safe and w.devices["bias"].safe)


# -- monitor and chart windows (DataHub) -----------------------------------------------------
def _sim_connected(app, window, ini="gui.ini"):
    """Sim station, all connected, no live timers: polls happen only when asked."""
    w = window("station.sim.toml", ini)
    for p in w.panels.values():
        p.live.setChecked(False)
    w.connect_all()
    wait(app, lambda: all(p.conn_state == "connected" for p in w.panels.values()))
    for p in w.panels.values():
        wait(app, lambda p=p: not p._reading)          # the first poll after connect
    return w


def _poll(app, panel):
    panel.poll()
    wait(app, lambda: not panel._reading)


def _count_reads(dev):
    calls = [0]
    orig = dev.read

    async def counted():
        calls[0] += 1
        return await orig()
    dev.read = counted
    return calls


def _fields(w, *keys, **alarms):
    from generalmeasurements.gui.views import ViewField
    out = []
    for k in keys:
        f = ViewField.of(w.hub.field(k))
        for attr, v in alarms.get(k, {}).items():
            setattr(f, attr, v)
        out.append(f)
    return out


def test_hub_lists_described_fields_and_keeps_latest(app, window):
    w = _sim_connected(app, window)
    keys = [f.key for f in w.hub.fields()]
    assert keys[:3] == ["gate", "bias", "magnet"] and {"lockin.x", "lockin.y"} <= set(keys)
    assert w.hub.field("magnet").units == "T"
    assert w.hub.latest("gate") is not None                 # from the first poll
    w.panels["gate"].disconnect_dev()
    app.processEvents()
    assert w.hub.latest("gate") is None and "gate" not in [f.key for f in w.hub.fields()]


def test_one_poll_updates_monitor_and_chart_without_extra_reads(app, window):
    w = _sim_connected(app, window)
    gate = w.panels["gate"]
    calls = _count_reads(w.devices["gate"])
    gate.target.setText("0.25")
    gate._go()
    wait(app, lambda: w.devices["gate"].value == 0.25)
    mon = w.open_view("monitor", _fields(w, "gate", "magnet"))
    chart = w.open_view("chart", _fields(w, "gate", "magnet"))
    chart2 = w.open_view("chart", _fields(w, "gate"))
    assert calls[0] == 0                                    # opening views reads nothing
    _poll(app, gate)
    assert calls[0] == 1                                    # one poll, one read()
    assert mon.view.value_text("gate") == "250 mV"
    assert chart.view.data["gate"][-1][1] == 0.25 and chart2.view.data["gate"][-1][1] == 0.25
    assert chart.view.right is not None                     # V and T: second y-axis
    for _ in range(3):
        _poll(app, gate)
    assert calls[0] == 4 and len(chart.view.data["gate"]) >= 4


def test_monitor_alarms_and_disconnect_resume(app, window):
    w = _sim_connected(app, window)
    gate = w.panels["gate"]
    mon = w.open_view("monitor", _fields(w, "gate", gate={"amber_high": 1.0, "red_high": 2.0}))
    style = mon.view.cards["gate"].value.styleSheet
    for v, color in ((0.5, None), (1.5, "#d68910"), (2.5, "#c0392b")):
        gate.target.setText(str(v))
        gate._go()
        wait(app, lambda v=v: w.devices["gate"].value == v)
        _poll(app, gate)
        assert (color in style()) if color else ("color:" not in style())
    gate.disconnect_dev()
    app.processEvents()
    assert mon.view.value_text("gate") == "—"
    assert "disconnected" in mon.view.cards["gate"].sub.text()
    gate.connect_dev()
    wait(app, lambda: gate.conn_state == "connected")
    wait(app, lambda: mon.view.value_text("gate") != "—")   # resumes on the next poll


def test_views_saved_and_restored_with_fields(app, window):
    w = _sim_connected(app, window, "views.ini")
    w.open_view("monitor", _fields(w, "gate", "lockin.x", gate={"red_low": -1.0}))
    w.open_view("chart", _fields(w, "magnet", "bias"))
    closed = w.open_view("chart", _fields(w, "gate"))
    closed.close()                                          # closed: not saved
    assert "Chart 2" not in w.views
    w.save_layout()
    w2 = window("station.sim.toml", "views.ini")            # same file, nothing connected
    assert set(w2.views) == {"Monitor 1", "Chart 1"}
    mon = w2.views["Monitor 1"]
    assert [f.key for f in mon.fields] == ["gate", "lockin.x"]
    assert mon.fields[0].red_low == -1.0
    assert mon.view.value_text("gate") == "—"               # instrument not connected
    ch = w2.views["Chart 1"]
    assert [f.key for f in ch.fields] == ["magnet", "bias"] and ch.view.right is not None
    w2.connect_all()                                        # the restored views come alive
    wait(app, lambda: mon.view.value_text("gate") != "—")


def test_reset_layout_forgets_saved_views(app, window, tmp_path):
    w = _sim_connected(app, window, "reset.ini")
    w.open_view("monitor", _fields(w, "gate"))
    w.save_layout()
    from generalmeasurements.gui.app import StationWindow
    fresh = StationWindow("station.sim.toml",
                          QtCore.QSettings(str(tmp_path / "reset.ini"),
                                           QtCore.QSettings.Format.IniFormat),
                          reset_layout=True)
    try:
        assert fresh.views == {}
    finally:
        fresh.worker.stop()


def test_field_picker_selection_and_validation(app, window):
    from generalmeasurements.gui.views import FieldPicker
    w = _sim_connected(app, window)
    dlg = FieldPicker("monitor", w.hub.fields())
    with pytest.raises(ValueError, match="at least one"):
        dlg.result_fields()
    dlg.check("magnet")
    dlg.set_alarm("magnet", "amber_high", "500m")
    (f,) = dlg.result_fields()
    assert (f.key, f.units, f.amber_high) == ("magnet", "T", pytest.approx(0.5))
    dlg.set_alarm("magnet", "red_low", "lots")
    with pytest.raises(ValueError, match="not a number"):
        dlg.result_fields()
    chart = FieldPicker("chart", w.hub.fields())
    for k in ("gate", "magnet"):
        chart.check(k)
    assert len(chart.result_fields()) == 2


def _fake(monkeypatch, make_dev):
    async def build(name, cfg, others):
        return make_dev(name)
    monkeypatch.setattr(panels, "build_device", build)


def test_keithley_panel_attach_does_not_reset(app, window, monkeypatch):
    t = FakeTransport({":SOUR:FUNC?": "CURR", ":OUTP?": "1", ":SOUR:CURR:LEV?": "1.05E-06",
                       ":READ?": "0.1,1.05e-6,0,0,0", ":SENS:VOLT:PROT:TRIP?": "0"})
    _fake(monkeypatch, lambda n: Keithley24xx(n, t, "2401", "curr", 0.2, (-2e-6, 2e-6), 1e-6))
    w = window("station.lab.toml")
    p = w.panels["bias"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    assert "*RST" not in t.log and not any(c.startswith(":SOUR:CURR:LEV ") for c in t.log)
    assert p.dev._output and p.dev._level == pytest.approx(1.05e-6)   # adopted, not assumed
    wait(app, lambda: p._labels["bias.i"][0].text() == "1.05 µA")
    assert w._dots["bias"].toolTip() == "connected"


def test_keithley_panel_mode_mismatch_disables_source(app, window, monkeypatch):
    t = FakeTransport({":SOUR:FUNC?": "CURR", ":OUTP?": "0"})
    _fake(monkeypatch, lambda n: Keithley24xx(n, t, "2400", "volt", 1e-6, (-1, 1), 0.1))
    w = window("station.lab.toml")
    p = w.panels["gate"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    assert p.rows["level"].controls and not any(c.isEnabled() for c in p.rows["level"].controls)
    assert not any(c.isEnabled() for c in p.rows["output"].controls)
    assert "the instrument is in Current source" in w.log_view.toPlainText()
    assert ":READ?" not in t.log                       # output off: no read attempted


def test_lakeshore625_ramp_asks_then_moves(app, window, monkeypatch):
    replies = {"*IDN?": "LSCI,MODEL625,LSA179P,1.3/1.1", "LIMIT?": "+49.6000,+5.0000,+1.0000",
               "SETI?": "+00.0000", "RDGI?": "-00.0001", "RDGV?": "-0.0123", "PSH?": "0",
               "RATE?": "+0.0350", "FLDS?": "1,0.1", "OPST?": "006", "ERST?": "000,000,000"}
    t = FakeTransport(replies)
    _fake(monkeypatch, lambda n: LakeShore625(n, t, (-10, 10), 0.035))
    w = window("station.lab.toml")
    p = w.panels["magnet"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    assert not any(c.startswith(("SETI ", "RATE ")) for c in t.log)
    p.target.setText("1")
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    p._ramp()
    app.processEvents()
    assert "SETI 1.0000" not in t.log                  # declined
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: YES)
    p._ramp()
    wait(app, lambda: "SETI 1.0000" in t.log)


def test_gigatronics_power_cap_enforced(app, window, monkeypatch):
    t = FakeTransport({"OUTP?": "0", "FREQ?": "7054000000.0", "POW?": "20.00",
                       "SYST:ERR?": "0, No error"})
    _fake(monkeypatch, lambda n: Gigatronics2420B(n, t, max_power_dbm=0.0))
    w = window("station.lab.toml")
    p = w.panels["rf"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    p.rows["power"].edit.setText("5")
    p._write_from_edit("power")
    wait(app, lambda: "max_power_dbm" in w.log_view.toPlainText())
    assert not any(c.startswith("POW ") for c in t.log)
    assert "max_power_dbm" in w.windows["RF source"].statusBar().currentMessage()


# -- the panel built from a driver's definition ----------------------------------------
class _SMU:
    """Replies like a 24xx that remembers its source function and output."""

    def __init__(self, func="VOLT"):
        self.func, self.out, self.log = func, "0", []

    def __call__(self, cmd):
        return {":SOUR:FUNC?": self.func, ":OUTP?": self.out, ":SYST:ERR?": "0,No error",
                ":READ?": "0.1,1e-07,0,0,0"}.get(cmd, "0")

    def transport(self):
        smu = self

        class T(FakeTransport):
            async def write(self, cmd):
                await super().write(cmd)
                if cmd.startswith(":SOUR:FUNC "):
                    smu.func = cmd.split()[1]
                if cmd.startswith(":OUTP "):
                    smu.out = "1" if cmd.endswith("ON") else "0"
        return T(self)


def test_definition_panel_output_asks_then_reads(app, window, monkeypatch):
    smu = _SMU()
    t = smu.transport()
    _fake(monkeypatch, lambda n: Keithley24xx(n, t, "2400", "volt", 1e-6, (-1, 1), 50.0,
                                              settle_s=0, step_dt=0.001))
    w = window("station.lab.toml")
    p = w.panels["gate"]
    assert isinstance(p, panels.DefinitionPanel)
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled() and "output" in p.rows)
    assert list(p.rows) == ["output", "level", "compliance", "nplc"]     # by group
    assert p.rows["level"].unit == "V" and p.rows["compliance"].unit == "A"
    assert ":READ?" not in t.log                       # read_if: output is off
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    p.write_prop("output", True)
    app.processEvents()
    assert ":OUTP ON" not in t.log                     # declined
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: YES)
    p.write_prop("output", True)
    wait(app, lambda: p._labels["gate.v"][0].text() == "100 mV")
    assert t.log.index(":SOUR:VOLT:LEV 0") < t.log.index(":OUTP ON")
    p.rows["level"].edit.setText("200m")
    p._write_from_edit("level")
    wait(app, lambda: p.rows["level"].now.text().startswith("200 mV · read"))
    assert ":SOUR:VOLT:LEV 0.2" in t.log


def test_definition_panel_switches_mode_with_station_settings(app, window, monkeypatch):
    from generalmeasurements.config.schema import K24xxModeCfg
    smu = _SMU("CURR")
    t = smu.transport()
    _fake(monkeypatch, lambda n: Keithley24xx(n, t, "2401", "curr", 0.2, (-2e-6, 2e-6), 1e-6))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: YES)
    w = window("station.lab.toml")
    p = w.panels["bias"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled() and "level" in p.rows)
    p.switch_mode("volt")                               # the station has no volt settings
    assert "add [devices.bias.modes.volt]" in w.log_view.toPlainText()
    assert "*RST" not in t.log
    p.cfg = p.cfg.model_copy(update={"modes": {"volt": K24xxModeCfg(
        compliance=1e-6, limits=(-1, 1), max_rate=0.1)}})
    p.switch_mode("volt")
    wait(app, lambda: p.defn is not None and p.defn.mode == "volt" and "level" in p.rows)
    assert ":SOUR:FUNC VOLT" in t.log and p.cfg.mode == "volt"
    assert (p.rows["level"].unit, p.dev.limits) == ("V", (-1.0, 1.0))
    assert p._mismatch is None and all(c.isEnabled() for c in p.rows["level"].controls)
    assert w.hub.is_up("bias")


def test_definition_panel_dmm_function_and_lockin_tables(app, window, monkeypatch):
    from generalmeasurements.devices import SR850, Keithley2000
    func = ['"FRES"']

    class Meter(FakeTransport):                         # reports the function last written
        async def write(self, cmd):
            await super().write(cmd)
            if cmd.startswith(":SENS:FUNC "):
                func[0] = '"' + cmd.split("'")[1] + '"'
    dmm_t = Meter(lambda c: {":SENS:FUNC?": func[0], ":READ?": "+1.0E+02",
                             ":SYST:ERR?": "0,No error"}.get(c, "0"))
    li_t = FakeTransport({"SENS?": "17", "OFLT?": "8", "*IDN?": "SR850", "FREQ?": "1000",
                          "SLVL?": "0.1", "PHAS?": "0", "SNAP? 1,2,3,4": "1e-6,0,1e-6,0"})
    devs = {"dmm": lambda n: Keithley2000(n, dmm_t, "volt"), "lockin": lambda n: SR850(n, li_t)}
    _fake(monkeypatch, lambda n: devs[n](n))
    w = window("station.lab.toml")
    dmm, li = w.panels["dmm"], w.panels["lockin"]
    dmm.connect_dev()
    li.connect_dev()
    wait(app, lambda: "range" in dmm.rows and "sensitivity" in li.rows)
    assert dmm.defn.mode == "fres" and dmm.rows["range"].unit == "Ohm"   # adopted on connect
    assert w.hub.field("dmm").units == "Ohm"
    dmm.switch_mode("volt")
    wait(app, lambda: dmm.defn.mode == "volt" and w.hub.field("dmm").units == "V")
    assert ":SENS:FUNC 'VOLT:DC'" in dmm_t.log
    sens = li.rows["sensitivity"].edit
    assert sens.count() == 27 and sens.currentText() == "1 mV"     # SENS? 17
    sens.setCurrentIndex(10)
    li.rows["sensitivity"].controls[1].click()
    wait(app, lambda: "SENS 10" in li_t.log)


def test_definition_panel_renders_a_cryostat_driver(app, window, monkeypatch):
    from generalmeasurements.devices import LakeShore335
    replies = {"RANGE? 1": "0", "*IDN?": "LSCI,MODEL335", "SETP? 1": "4.0", "RAMP? 1": "0,2",
               "OUTMODE? 1": "1,1,0", "PID? 1": "50,20,0", "RDGST? A": "0", "KRDG? A": "4.2",
               "HTR? 1": "0.0"}
    t = FakeTransport(replies)
    _fake(monkeypatch, lambda n: LakeShore335(n, t, "A", 1, (2, 300), 0.0333))
    w = window("station.lab.toml")
    p = panels.DefinitionPanel("temp", w.cfg.devices["temp"], w)
    p.connect_dev()
    wait(app, lambda: "heater_range" in p.rows and p.rows["heater_range"].now.text() == "Off")
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or YES)
    p.write_prop("heater_range", 0)
    wait(app, lambda: "RANGE 1,0" in t.log)
    assert asked == []                                  # Off: no question
    p.write_prop("heater_range", 3)
    wait(app, lambda: "RANGE 1,3" in t.log)
    assert asked == ["Set the loop 1 heater range to High?"]
    p.timer.stop()
