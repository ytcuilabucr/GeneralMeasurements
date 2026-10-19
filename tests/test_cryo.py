"""LM-510 driver, transport echo handling, and the cryostat panels (offscreen)."""

import math
import os
import time

import pytest

from gmcore.devices import LM510, DriverError, FakeTransport, VisaTransport

LM = {"*IDN?": "Cryomagnetics,LM-510,1234,1.70", "UNITS?": "in", "MEAS? 1": "23.4 in",
      "MODE?": "S", "INTVL?": "01:00:00", "LNGTH?": "35.0 in", "LOW?": "5.0 in",
      "HIGH?": "30.0 in"}


# -- LM-510 driver ----------------------------------------------------------------------
async def test_lm510_reads_last_level_without_measuring():
    t = FakeTransport(LM)
    m = LM510("he", t)
    await m.configure()
    assert await m.read() == {"he": 23.4}
    assert "MEAS 1" not in t.log                         # no measurement started
    snap = await m.snapshot()
    assert (snap["mode"], snap["interval_s"], snap["low"], snap["high"]) == (
        "sample/hold", 3600, 5.0, 30.0)


async def test_lm510_refuses_other_units_and_models():
    with pytest.raises(DriverError, match="reports in 'cm'"):
        await LM510("he", FakeTransport({**LM, "UNITS?": "CM"})).configure()
    with pytest.raises(DriverError, match="LM-510"):
        await LM510("he", FakeTransport({**LM, "*IDN?": "LSCI,MODEL335,1,1"})).configure()
    with pytest.raises(DriverError, match="'%'"):
        await LM510("he", FakeTransport({**LM, "MEAS? 1": "66.9 %"})).read()


async def test_lm510_mode_interval_measure_commands():
    t = FakeTransport(LM)
    m = LM510("he", t, channel=1)
    await m.set_mode("continuous")
    await m.set_interval(90 * 60)
    await m.measure()
    assert t.log == ["MODE C", "INTVL 01:30:00", "MEAS 1"]
    with pytest.raises(DriverError):
        await m.set_mode("fast")


class _EchoInst:
    """A pyvisa stand-in that echoes every command line before its reply."""

    def __init__(self, replies):
        self.replies, self.lines, self.timeout = replies, [], 5000

    def write(self, cmd):
        self.lines.append(cmd)
        if cmd in self.replies:
            self.lines.append(self.replies[cmd])

    def read(self):
        import pyvisa
        if not self.lines:
            raise pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
        return self.lines.pop(0)

    def clear(self):
        self.lines.clear()


async def test_transport_skips_echoed_commands():
    pytest.importorskip("pyvisa")
    t = VisaTransport("TCPIP0::x::4266::SOCKET", echo=True, echo_wait_ms=10)
    t._inst = _EchoInst({"MEAS? 1": "23.4 in", "*IDN?": "Cryomagnetics,LM-510"})
    await t.write("MODE S")                  # its echo is read and dropped
    assert await t.query("MEAS? 1") == "23.4 in"
    assert await t.query("*IDN?") == "Cryomagnetics,LM-510"
    t._inst = _EchoInst({})
    await t.write("MEAS 1")                  # no echo: waits echo_wait_ms, no error


# -- panels -----------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def gui():
    pytest.importorskip("PySide6QtAds")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtCore, QtWidgets

    from gmcore.gui.common import panels
    from gmcore.gui.legacy.station import StationWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    made = []

    def open_panel(name, make_dev, tmp_path, monkeypatch):
        async def build(n, cfg, others):
            return make_dev(n)
        monkeypatch.setattr(panels, "build_device", build)
        w = StationWindow("tests/fixtures/lab.toml",
                          QtCore.QSettings(str(tmp_path / "g.ini"),
                                           QtCore.QSettings.Format.IniFormat))
        made.append(w)
        p = w.panels[name]
        p.connect_dev()
        wait(app, lambda: p.conn_state == "connected")
        return w, p

    def wait(app_, cond, timeout=3.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            app_.processEvents()
            if cond():
                return
            time.sleep(0.005)
        raise AssertionError("condition not met in time")
    yield app, open_panel, lambda cond: wait(app, cond)
    for w in made:
        for p in w.panels.values():
            p.timer.stop()
        w.worker.stop()


def test_helium_panel_level_boiloff_and_mode(gui, tmp_path, monkeypatch):
    _app, open_panel, wait = gui
    from PySide6 import QtWidgets
    t = FakeTransport(LM)
    w, p = open_panel("helium", lambda n: LM510(n, t), tmp_path, monkeypatch)
    wait(lambda: p.level.value.text() == "23.4 in")
    assert p.dewar.level == 23.4 and p.dewar.low == 5.0 and p.dewar.full == 35.0
    assert p.mode_sh.isChecked()
    assert w.windows["Helium level"].panels == [p]
    now = time.time()                        # falling 2 in/day for the last 6 h
    p.history.clear()
    for k in range(12):
        p.history.append((now - 6 * 3600 + k * 1800, 24.0 - k * 2 / 48))
    p.show_data({"helium": 24.0 - 12 * 2 / 48})
    assert p.boil.value.text() == "2.00 in/day"
    days = float(p.left.value.text().split()[0])          # (23.5 - 5) in / 2 in/day
    assert days == pytest.approx(9.25, abs=0.06) and p.left.value.text().endswith("days")
    p.mode_c.setChecked(True)
    p._mode()
    wait(lambda: "MODE C" in t.log)
    assert isinstance(p.mode_box, QtWidgets.QGroupBox)


def test_temperature_panel_both_inputs_and_range_confirm(gui, tmp_path, monkeypatch):
    app, open_panel, wait = gui
    from PySide6 import QtWidgets

    from gmcore.devices import LakeShore335
    replies = {"*IDN?": "LSCI,MODEL335,LSA1789/#######,1.4", "OUTMODE? 1": "1,1,0",
               "RDGST? A": "0", "KRDG? A": "+77.589", "RDGST? B": "0", "KRDG? B": "+4.2310",
               "SETP? 1": "+80.000", "HTR? 1": "+12.5", "RAMPST? 1": "1", "RANGE? 1": "0",
               "RAMP? 1": "1,+2.0", "PID? 1": "+50.0,+20.0,+0.0"}
    t = FakeTransport(replies)
    _w, p = open_panel("temp", lambda n: LakeShore335(n, t, "A", 1, (2, 300), 0.0333),
                      tmp_path, monkeypatch)
    wait(lambda: p.card["B"].value.text() == "4.231 K")
    assert p.card["A"].value.text() == "77.589 K" and "control" in p.card["A"].caption.text()
    assert "ramping" in p.ramp_state.text() and p.heat_bar.value() == 125
    assert p.range_group.button(0).isChecked()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No)
    p.range_group.button(3).click()
    app.processEvents()
    assert "RANGE 1,3" not in t.log                     # declined: nothing written
    assert not any(c.startswith(("PID ", "OUTMODE ")) for c in t.log)


def test_magnet_panel_status_lights_and_zero_ramp(gui, tmp_path, monkeypatch):
    _app, open_panel, wait = gui
    from PySide6 import QtWidgets

    from gmcore.devices import LakeShore625
    replies = {"*IDN?": "LSCI,MODEL625,LSA179P,1.3/1.1", "LIMIT?": "+49.6000,+5.0000,+1.0000",
               "SETI?": "+02.0000", "RDGI?": "+01.5000", "RDGV?": "+0.0123", "PSH?": "1",
               "RATE?": "+0.0350", "FLDS?": "1,0.1", "OPST?": "006", "ERST?": "000,000,000"}
    t = FakeTransport(replies)
    _w, p = open_panel("magnet", lambda n: LakeShore625(n, t, (-10, 10), 0.035),
                      tmp_path, monkeypatch)
    wait(lambda: p.led_ramp.state == "busy")
    assert p.led_psh.state == "warn" and p.led_err.state == "on"
    assert p.bar.target == 2.0 and p.bar.value == 1.5 and "ETA" in p.eta.text()
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    p._zero()
    wait(lambda: "SETI 0.0000" in t.log)
    assert not any(c.startswith(("PSH ", "LIMIT ")) for c in t.log)
    assert math.isclose(p.dev.target, 0.0)
