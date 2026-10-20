"""GUI on simulators and fake transports, offscreen (no display, no hardware)."""

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ads = pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
from PySide6 import QtCore, QtWidgets  # noqa: E402

from gmcore.devices import (FakeTransport, Gigatronics2420B, Keithley2400,  # noqa: E402
                            Keithley2401, LakeShore625)
from gmcore.gui.common import panels  # noqa: E402
from gmcore.gui.legacy.station import StationWindow  # noqa: E402
from gmcore.gui.common.panels import fmt, parse_si  # noqa: E402

YES = QtWidgets.QMessageBox.StandardButton.Yes

# the range queries a Keithley panel makes on connect: "autoranging"
AUTORANGE = {f":{k}:{f}:RANG:AUTO?": "1" for k in ("SOUR", "SENS") for f in ("VOLT", "CURR")}

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
    w = window("tests/fixtures/lab.toml")
    groups = {t: [p.name for p in iw.panels] for t, iw in w.windows.items()}
    assert groups == {"Keithley": ["gate", "bias", "dmm"], "Lock-in": ["lockin"],
                      "Temperature": ["temp"], "Magnet": ["magnet"], "RF source": ["rf"],
                      "Helium level": ["helium"]}
    k = w.windows["Keithley"]
    assert all(k.dock(n).isTabbed() for n in ("gate", "bias", "dmm"))   # one tabbed area
    assert k.dock("gate").dockAreaWidget() is k.dock("dmm").dockAreaWidget()


def test_window_key_overrides_grouping(app, window, tmp_path):
    src = open("tests/fixtures/sim.toml", encoding="utf-8").read()
    src = src.replace('driver = "sim_magnet"', 'driver = "sim_magnet"\nwindow = "Sources"')
    (tmp_path / "st.toml").write_text(src, "utf-8")
    w = window(tmp_path / "st.toml")
    assert [p.name for p in w.windows["Sources"].panels] == ["gate", "bias", "magnet"]
    assert "Magnet" not in w.windows


def test_layout_saved_and_restored(app, window):
    w = window("tests/fixtures/lab.toml")
    w.show()
    k = w.windows["Keithley"]
    area = k.dock("gate").dockAreaWidget()
    k.docks.addDockWidget(ads.RightDockWidgetArea, k.dock("dmm"), area)
    assert not k.dock("dmm").isTabbed()                 # split out beside the others
    w.windows["Magnet"].close()
    w.save_layout()
    w2 = window("tests/fixtures/lab.toml")                     # same ini file
    k2 = w2.windows["Keithley"]
    assert not k2.dock("dmm").isTabbed() and k2.dock("gate").isTabbed()
    w2.show()
    assert not w2.windows["Magnet"].isVisible() and k2.isVisible()
    w2.reset_layout()
    assert k2.dock("dmm").isTabbed()


def test_callbacks_run_on_the_gui_thread(app, window):
    w = window("tests/fixtures/sim.toml")
    seen = []

    async def where():
        return threading.get_ident()
    w.worker.submit(where(), lambda r: seen.append((r, threading.get_ident())))
    wait(app, lambda: seen)
    loop_thread, callback_thread = seen[0]
    assert loop_thread != callback_thread == threading.get_ident()


def test_sim_station_connect_set_and_ramp(app, window, monkeypatch):
    w = window("tests/fixtures/sim.toml")
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
    w = window("tests/fixtures/sim.toml", ini)
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
    from gmcore.gui.legacy.views import ViewField
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
    w2 = window("tests/fixtures/sim.toml", "views.ini")            # same file, nothing connected
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
    from gmcore.gui.legacy.station import StationWindow
    fresh = StationWindow("tests/fixtures/sim.toml",
                          QtCore.QSettings(str(tmp_path / "reset.ini"),
                                           QtCore.QSettings.Format.IniFormat),
                          reset_layout=True)
    try:
        assert fresh.views == {}
    finally:
        fresh.worker.stop()


def test_field_picker_selection_and_validation(app, window):
    from gmcore.gui.legacy.views import FieldPicker
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
    t = FakeTransport({**AUTORANGE, ":SOUR:FUNC?": "CURR", ":OUTP?": "1", ":SOUR:CURR:LEV?": "1.05E-06",
                       ":READ?": "0.1,1.05e-6,0,0,0", ":SENS:VOLT:PROT:TRIP?": "0"})
    _fake(monkeypatch, lambda n: Keithley2401(n, t, "curr", 0.2, (-2e-6, 2e-6)))
    w = window("tests/fixtures/lab.toml")
    p = w.panels["bias"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    assert "*RST" not in t.log and not any(c.startswith(":SOUR:CURR:LEV ") for c in t.log)
    assert p.dev._output and p.dev._level == pytest.approx(1.05e-6)   # adopted, not assumed
    wait(app, lambda: p._labels["bias.i"][0].text() == "1.05 µA")
    assert w._dots["bias"].toolTip() == "connected"


def test_keithley_panel_mode_mismatch_disables_source(app, window, monkeypatch):
    t = FakeTransport({**AUTORANGE, ":SOUR:FUNC?": "CURR", ":OUTP?": "0"})
    _fake(monkeypatch, lambda n: Keithley2400(n, t, "volt", 1e-6, (-1, 1)))
    w = window("tests/fixtures/lab.toml")
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
    w = window("tests/fixtures/lab.toml")
    p = w.panels["magnet"]
    p.connect_dev()
    wait(app, lambda: p.btn_read.isEnabled())           # connected (no Control box here)
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
    w = window("tests/fixtures/lab.toml")
    p = w.panels["rf"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled())
    p.rows["power"].edit.setText("5")
    p._write_from_edit("power")                      # the panel checks the definition's max
    assert "refused: allowed at most 0 dBm; not sent" in w.log_view.toPlainText()
    p.write_prop("power", 5.0)                       # ... and the driver checks again
    wait(app, lambda: "max_power_dbm" in w.log_view.toPlainText())
    assert not any(c.startswith("POW ") for c in t.log)
    assert "max_power_dbm" in w.windows["RF source"].statusBar().currentMessage()


# -- the panel built from a driver's definition ----------------------------------------
class _SMU:
    """Replies like a Keithley 2400 that remembers its source function, output and ranges."""

    def __init__(self, func="VOLT"):
        self.func, self.out, self.log = func, "0", []
        self.ranges: dict[str, str] = {}             # ":SOUR:VOLT" -> "2"; absent = auto

    def __call__(self, cmd):
        for k, v in self.ranges.items():
            if cmd == f"{k}:RANG:AUTO?":
                return "0"
            if cmd == f"{k}:RANG?":
                return v
        return {**AUTORANGE, ":SOUR:FUNC?": self.func, ":OUTP?": self.out,
                ":SYST:ERR?": "0,No error", ":READ?": "0.1,1e-07,0,0,0"}.get(cmd, "0")

    def transport(self):
        smu = self

        class T(FakeTransport):
            async def write(self, cmd):
                await super().write(cmd)
                if cmd.startswith(":SOUR:FUNC "):
                    smu.func = cmd.split()[1]
                if cmd.startswith(":OUTP "):
                    smu.out = "1" if cmd.endswith("ON") else "0"
                head, _, arg = cmd.partition(":RANG")
                if arg.startswith(":AUTO ON"):
                    smu.ranges.pop(head, None)
                elif arg.startswith(" "):
                    smu.ranges[head] = arg.strip()
        return T(self)


def test_definition_panel_output_asks_then_reads(app, window, monkeypatch):
    smu = _SMU()
    t = smu.transport()
    _fake(monkeypatch, lambda n: Keithley2400(n, t, "volt", 1e-6, (-1, 1),
                                              settle_s=0))
    w = window("tests/fixtures/lab.toml")
    p = w.panels["gate"]
    assert isinstance(p, panels.DefinitionPanel)
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled() and "output" in p.rows)
    assert list(p.rows) == ["output", "level",                            # level 0
                            "compliance", "source_range", "effective_compliance",
                            "sense_range",                               # 1, by group
                            "nplc"]                                      # 2
    src = p.rows["source_range"].edit                     # this model's ranges + Auto
    assert [src.itemText(i) for i in range(src.count())] == [
        "Auto", "200 mV", "2 V", "20 V", "200 V"]
    assert src.currentText() == "Auto" and p.rows["sense_range"].now.text() == "auto"
    assert all(c.isEnabled() for c in p.rows["source_range"].controls)   # output off
    src.setCurrentText("2 V")
    p.rows["source_range"].controls[1].click()                          # Set
    wait(app, lambda: p.rows["source_range"].now.text() == "2 V")       # read back
    assert ":SOUR:VOLT:RANG 2" in t.log
    sense = p.rows["sense_range"].edit
    sense.setCurrentText("10 µA")
    p.rows["sense_range"].controls[1].click()
    wait(app, lambda: p.rows["sense_range"].now.text() == "10 µA")       # read back
    assert ":SENS:CURR:RANG 1e-05" in t.log
    assert p.rows["effective_compliance"].now.text() == "1 µA"   # 1 µA < 10.5 µA: unchanged
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
    wait(app, lambda: not any(c.isEnabled() for c in p.rows["source_range"].controls))
    assert all(c.isEnabled() for c in p.rows["sense_range"].controls)  # allowed while on
    p.rows["level"].edit.setText("200m")
    p._write_from_edit("level")
    wait(app, lambda: p.rows["level"].now.text().startswith("200 mV · read"))
    assert ":SOUR:VOLT:LEV 0.2" in t.log


def test_definition_panel_switch_mode_asks_for_the_new_settings(app, window, monkeypatch):
    from gmcore.config.schema import KeithleySMUModeCfg
    smu = _SMU("CURR")
    t = smu.transport()
    _fake(monkeypatch, lambda n: Keithley2401(n, t, "curr", 0.2, (-2e-6, 2e-6)))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: YES)
    w = window("tests/fixtures/lab.toml")
    p = w.panels["bias"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled() and "level" in p.rows)
    asked = []

    def answer(target, keys, start, units, message):  # stands in for the dialog
        asked.append((target, start, units))
        return {"compliance": 1e-6, "limits": (-1.0, 1.0)}
    monkeypatch.setattr(p, "ask_switch_settings", answer)
    assert p._switch_btn.isEnabled()                    # connected, output off
    p.values["output"] = True                           # output on: no switching
    p._update_enabled()
    assert not p._switch_btn.isEnabled()
    assert p._switch_btn.toolTip() == "Turn output off first"
    p.switch_mode("volt")
    assert "turn Output off before switching" in w.log_view.toPlainText() and not asked
    p.values["output"], p.values["level"] = False, 1e-6  # off; the level doesn't matter
    p._update_enabled()
    p._switch_btn.click()                               # the button; no table: defaults
    assert asked[0] == ("volt", {"compliance": 1e-6, "limits": [-1.0, 1.0]},
                        {"compliance": "A", "limits": "V"})
    wait(app, lambda: p.defn is not None and p.defn.mode == "volt" and "level" in p.rows)
    assert ":SOUR:FUNC VOLT" in t.log and p.cfg.mode == "volt"
    assert (p.rows["level"].unit, p.dev.limits) == ("V", (-1.0, 1.0))
    assert p._mismatch is None and all(c.isEnabled() for c in p.rows["level"].controls)
    assert w.hub.is_up("bias")
    p.cfg = p.cfg.model_copy(update={"modes": {"curr": KeithleySMUModeCfg(
        compliance=0.5, limits=(-1e-6, 1e-6))}})
    p.switch_mode("curr")                               # the station's table pre-fills
    assert asked[1][1] == {"compliance": 0.5, "limits": (-1e-6, 1e-6)}


def test_definition_panel_dmm_function_and_lockin_tables(app, window, monkeypatch):
    from gmcore.devices import SR850, Keithley2000
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
    w = window("tests/fixtures/lab.toml")
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


def test_definition_panel_folds_by_level(app, window, monkeypatch):
    from gmcore.devices import SR850
    li_t = FakeTransport({"SENS?": "17", "OFLT?": "8", "*IDN?": "SR850", "FREQ?": "1000",
                          "SLVL?": "0.1", "PHAS?": "0", "SNAP? 1,2,3,4": "1e-6,0,2e-6,45"})
    _fake(monkeypatch, lambda n: SR850(n, li_t))
    w = window("tests/fixtures/lab.toml")
    li, win = w.panels["lockin"], w.windows["Lock-in"]
    li.connect_dev()
    wait(app, lambda: "sensitivity" in li.rows and len(li.folds) == 2)
    more, every = li.folds
    assert every.isAncestorOf(li.rows["idn"].now) and more.body.isAncestorOf(every)  # nested
    assert more.body.isAncestorOf(li.rows["frequency"].now)
    assert not more.isAncestorOf(li.rows["sensitivity"].now)                   # level 0
    assert li.rows["sensitivity"].now.isVisibleTo(li)
    assert not li.rows["frequency"].now.isVisibleTo(li)                        # folded
    wait(app, lambda: li._labels["lockin.r"][0].text() == "2 µV")
    for key in ("lockin.x", "lockin.y", "lockin.r", "lockin.theta"):   # all main readings
        assert li.read_box.isAncestorOf(li._labels[key][0]), key
    win.set_detail(1)
    assert li.rows["frequency"].now.isVisibleTo(li) and not li.rows["idn"].now.isVisibleTo(li)
    win.detail_actions[2].trigger()
    assert li.detail == 2 and li.rows["idn"].now.isVisibleTo(li)
    more.set_open(False)                                # by hand: All stays open inside
    assert li.detail == 0 and every.is_open()
    more.set_open(True)
    assert li.detail == 2
    win.set_detail(1)
    w.save_layout()
    w2 = window("tests/fixtures/lab.toml")                     # same ini file
    assert w2.panels["lockin"].detail == 1 and w2.windows["Lock-in"].detail_actions[1].isChecked()
    assert w2.panels["gate"].detail == 0                # Keithley window: not changed
    assert not w2.windows["Temperature"].detail_actions   # own layout, no folds
    li.timer.stop()


def test_definition_panel_renders_a_cryostat_driver(app, window, monkeypatch):
    from gmcore.devices import LakeShore335
    replies = {"RANGE? 1": "0", "*IDN?": "LSCI,MODEL335", "SETP? 1": "4.0", "RAMP? 1": "0,2",
               "OUTMODE? 1": "1,1,0", "PID? 1": "50,20,0", "RDGST? A": "0", "KRDG? A": "4.2",
               "HTR? 1": "0.0"}
    t = FakeTransport(replies)
    _fake(monkeypatch, lambda n: LakeShore335(n, t, "A", 1, (2, 300), 0.0333))
    w = window("tests/fixtures/lab.toml")
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


def test_charts_folded_one_per_channel_up_to_four(app, window, monkeypatch):
    from gmcore.devices import SR850
    li_t = FakeTransport({"SENS?": "17", "OFLT?": "8", "*IDN?": "SR850", "FREQ?": "1000",
                          "SLVL?": "0.1", "PHAS?": "0", "SNAP? 1,2,3,4": "1e-6,2e-6,3e-6,45"})
    smu = _SMU()
    devs = {"lockin": lambda n: SR850(n, li_t),
            "gate": lambda n: Keithley2400(n, smu.transport(), "volt", 1e-6, (-1, 1))}
    _fake(monkeypatch, lambda n: devs[n](n))
    w = window("tests/fixtures/lab.toml")
    li, gate = w.panels["lockin"], w.panels["gate"]
    li.connect_dev()
    gate.connect_dev()
    wait(app, lambda: len(li.charts) == 4 and len(gate.charts) == 2)   # X Y R θ; V I
    assert not any(c.fold.is_open() for c in li.charts)                # hidden by default
    assert not li.charts[0].plot.isVisibleTo(li)
    assert [c.channel() for c in li.charts] == ["lockin.x", "lockin.y", "lockin.r",
                                                "lockin.theta"]
    assert li.charts[0].combo.itemText(0) == "lockin X (V)"
    li.poll()
    wait(app, lambda: len(li._history["lockin.r"]) >= 1)    # recorded while folded
    assert li.charts[0].curve.getData()[0] is None           # but not drawn
    c = li.charts[0]
    c.fold.set_open(True)
    assert c.plot.isVisibleTo(li) and list(c.curve.getData()[1]) == [1e-6]
    c.combo.setCurrentIndex(3)                               # θ on chart 1
    assert c.channel() == "lockin.theta" and list(c.curve.getData()[1]) == [45.0]
    assert c.plot.getPlotItem().getAxis("left").labelText == "lockin phase"
    c.plot.getPlotItem().setXRange(100, 200)                 # the user zooms in ...
    assert not c.plot.getPlotItem().getViewBox().autoRangeEnabled()[0]
    c.zoom.click()                                           # ... Auto zoom goes back
    assert all(c.plot.getPlotItem().getViewBox().autoRangeEnabled())
    for p in (li, gate):
        p.timer.stop()


def test_update_settings_nplc_box_and_resizable_charts(app, window, monkeypatch):
    smu = _SMU()
    t = smu.transport()
    _fake(monkeypatch, lambda n: Keithley2400(n, t, "volt", 1e-6, (-1, 1), settle_s=0))
    w = window("tests/fixtures/lab.toml")
    p = w.panels["gate"]
    p.connect_dev()
    wait(app, lambda: "nplc" in p.rows and len(p.charts) == 2)
    # Update settings: in the Control box, re-reads the settings with queries only
    assert p.ctrl_box.isAncestorOf(p.btn_update)
    sent = len(t.log)
    p.btn_update.click()
    wait(app, lambda: len(t.log) > sent)
    assert all(c.endswith("?") for c in t.log[sent:])
    # NPLC: a number box, bounded by min / max in keithley_2400.toml
    nplc = p.rows["nplc"]
    assert isinstance(nplc.edit, panels.SIEdit) and "limits 0.01 … 10" in nplc.edit.parent().toolTip()
    nplc.edit.setText("20")
    p._write_from_edit("nplc")
    assert "refused: allowed 0.01 … 10" in w.log_view.toPlainText()
    nplc.edit.setText("0.5")
    p._write_from_edit("nplc")
    wait(app, lambda: ":SENS:CURR:NPLC 0.5" in t.log)
    # the level is a plain Set: no rate box, no Stop
    assert p.rows["level"].rate is None
    assert [b.text() for b in p.rows["level"].controls if isinstance(b, QtWidgets.QPushButton)] \
        == ["Set"]
    # charts: panes of a splitter below the controls; closed ones only as tall as their heading
    assert p.charts_split.count() == 2 and p.main_split.widget(1) is p.charts_split
    closed = p.charts[0].fold
    assert closed.maximumHeight() < 60
    closed.set_open(True)
    assert closed.maximumHeight() > 10000
    p.timer.stop()


def test_panel_shows_how_long_a_read_takes(app, window):
    import asyncio
    w = _sim_connected(app, window)
    p = w.panels["gate"]
    assert len(p.read_times) == 1 and p.read_time.text().startswith("read ")
    dev, orig = p.dev, p.dev.read

    async def slow():
        await asyncio.sleep(0.18)
        return await orig()
    dev.read = slow
    p.interval.setValue(1.0)
    for _ in range(10):
        _poll(app, p)
    assert len(p.read_times) == 10 and p.read_time_s() >= 0.18   # the last ten only
    assert "color" not in p.read_time.text()
    p.interval.setValue(0.2)                            # reads take most of 0.2 s: amber
    assert "#b9770e" in p.read_time.text() and "no faster" in p.read_time.toolTip()


def test_aotf_panel_switches_calibration(app, window, monkeypatch, tmp_path):
    from gmcore.devices import AotfOctal
    from gmcore.devices.aotf_octal import read_calibration
    vis, nir = (Path(f"Instrument config/aotf/102102 {m}.aotf").resolve().as_posix()
                for m in ("Visible", "NIR1"))
    station = tmp_path / "optics.toml"
    station.write_text(f'''[station]
name = "optics"
shutdown_order = []

[devices.AOTF]
driver = "aotf_octal"
resource = "AOTF::0"
calibrations = {{ vis = "{vis}", nir = "{nir}" }}
''')
    f532 = read_calibration(vis).frequency(532.0) * 1e6
    t = FakeTransport({"dds f 0": f"Channel 0 profile 0 frequency {f532:.6e}Hz",
                       "dds a 0": "Channel 0 amplitude 624"})   # 10 % on
    _fake(monkeypatch, lambda n: AotfOctal(n, t, {"vis": read_calibration(vis),
                                                  "nir": read_calibration(nir)}))
    w = window(str(station))
    p = w.panels["AOTF"]
    p.connect_dev()
    wait(app, lambda: p.ctrl_box.isEnabled() and "wavelength" in p.rows)
    assert p.values["wavelength"] == pytest.approx(532.0, abs=1e-3)
    assert p.values["power"] == pytest.approx(10.0)
    monkeypatch.setattr(p, "ask_switch_settings", lambda *a: pytest.fail("no dialog"))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *a, **k: pytest.fail("no ask"))
    p._update_enabled()
    assert p._switch_btn.isEnabled()                    # light on: no interlock
    n = len(t.log)
    p._switch_btn.click()                               # one click, straight to NIR
    wait(app, lambda: p.defn is not None and p.defn.mode == "nir" and "wavelength" in p.rows)
    assert t.log[n:] == ["dds f 0", "dds a 0"]          # only the re-read: nothing written
    assert p.cfg.mode == "nir" and p.dev.cal_name == "102102 NIR1.aotf"
    # the same RF, read through the NIR curve; power relative to NIR1's band
    nm = p.dev.wavelength_of(f532 / 1e6)
    assert p.values["wavelength"] == pytest.approx(nm)
    assert p.values["power"] == pytest.approx(100 * 624 / p.dev.cal.norm(nm))
    p._switch_btn.click()                               # and back
    wait(app, lambda: p.defn.mode == "vis" and "wavelength" in p.rows)
    assert p.values["wavelength"] == pytest.approx(532.0, abs=1e-3)
