"""Instrument Server (gui/server/), offscreen with fake instruments."""

import os
import shutil
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
pytest.importorskip("tomlkit")
from PySide6 import QtWidgets  # noqa: E402

from gmcore.config import load_station  # noqa: E402
from gmcore.config.schema import check_name  # noqa: E402
from gmcore.config.registry import DRIVER_CLASSES  # noqa: E402
from gmcore.devices import FakeTransport  # noqa: E402
from gmcore.gui.common import panels  # noqa: E402
from gmcore.gui.common.dedicated_panels import CRYO_PANELS  # noqa: E402
from gmcore.gui.server import InstrumentServer, default_name  # noqa: E402

LAB = "tests/fixtures/instruments.toml"

# the range queries a Keithley panel makes on connect: "autoranging"
AUTORANGE = {f":{k}:{f}:RANG:AUTO?": "1" for k in ("SOUR", "SENS") for f in ("VOLT", "CURR")}

@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def server(app, tmp_path):
    shutil.copy(LAB, tmp_path / "lab.toml")
    servers = []

    def make(path=tmp_path / "lab.toml"):
        s = InstrumentServer(path)
        servers.append(s)
        return s
    yield make
    for s in servers:
        for r in s.rows:
            if r.panel is not None:
                r.panel.timer.stop()
            if r.window is not None:
                r.window.hide()
        s._dirty = False
        s.hide()
        s.worker.stop()


def wait(app, cond, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        app.processEvents()
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def _fake_smu(monkeypatch):
    t = FakeTransport({**AUTORANGE, ":SOUR:FUNC?": "VOLT", ":OUTP?": "0", ":SOUR:VOLT:LEV?": "0"})

    async def build(name, cfg, others):
        return DRIVER_CLASSES[cfg.driver](name, t, cfg.mode, cfg.compliance, cfg.limits)
    monkeypatch.setattr(panels, "build_device", build)
    return t


def test_shipped_configs_are_valid():
    for f in ("lab", "sim"):
        cfg = load_station(f"Instrument config/{f}.toml")
        assert set(cfg.station.shutdown_order) <= set(cfg.devices)
    assert "Keithley 2400 - 1" in load_station(LAB).devices


def test_names():
    assert default_name("Keithley 2400", {"Keithley 2400 - 1"}) == "Keithley 2400 - 2"
    assert default_name("SR850", set()) == "SR850 - 1"
    assert check_name("Keithley 2400 - 1")
    for bad in ("", " x", "a.b", "a/b", 'a"b'):
        with pytest.raises(ValueError):
            check_name(bad)


def test_rows_follow_the_config(server):
    s = server()
    assert [(r.address.text(), r.name_edit.text(), r.driver_menu.current_data()) for r in s.rows][:3] \
        == [("GPIB0::26::INSTR", "Keithley 2400 - 1", "keithley_2400"),
            ("GPIB0::22::INSTR", "Keithley 2401 - 1", "keithley_2401"),
            ("GPIB0::11::INSTR", "Keithley 2000 - 1", "keithley_2000")]
    r = s.rows[0]
    assert r.btn_connect.text() == "Connect" and "#1e8449" in r.btn_connect.styleSheet()
    assert r.led.state == "off"
    assert not s.btn_add.isEnabled()                    # not built yet
    menu = s.menuBar().actions()[0].menu()
    assert [a.text() for a in menu.actions() if a.text()] == ["&Open…", "&Save", "Save &as…",
                                                              "E&xit"]


def test_connect_disconnect_and_panel(server, app, monkeypatch):
    t = _fake_smu(monkeypatch)
    s = server()
    r = s.row("Keithley 2400 - 1")
    r.btn_connect.click()
    wait(app, lambda: r.state == "connected")
    assert "*RST" not in t.log and s.devices["Keithley 2400 - 1"] is r.panel.dev
    assert r.led.state == "on"
    assert r.btn_connect.text() == "Disconnect" and "#c0392b" in r.btn_connect.styleSheet()
    assert not r.name_edit.isEnabled() and not r.driver_menu.isEnabled()   # fixed while connected
    assert s.rename(r, "other") is False and r.name == "Keithley 2400 - 1"
    w = s.open_panel(r)
    assert isinstance(w.panel, panels.DefinitionPanel) and w.panel is r.panel
    assert w.detail_actions and s.open_panel(r) is w   # one window per instrument
    w.close()                                           # hides; still connected
    assert r.connected and not w.isVisible()
    r.btn_connect.click()
    wait(app, lambda: r.state == "disconnected")
    assert "Keithley 2400 - 1" not in s.devices and r.name_edit.isEnabled()
    assert r.btn_connect.text() == "Connect"


def test_open_panel_uses_the_dedicated_one(server):
    s = server()
    w = s.open_panel(s.row("Lake Shore 335 - 1"))
    assert type(w.panel) is CRYO_PANELS["lakeshore_335"] and not w.detail_actions
    assert w.panel.dev is None                          # opening a panel doesn't connect


def test_rename_and_driver_change_then_save(server, tmp_path):
    s = server()
    r = s.row("Keithley 2400 - 1")
    assert s.rename(r, "gate")
    assert not s.rename(s.row("Keithley 2401 - 1"), "gate")         # taken
    assert not s.rename(s.row("Keithley 2401 - 1"), "bias.v")       # data-key separator
    r.name_edit.setText("")
    assert s.rename(r, "") and r.name == "Keithley 2400 - 1"         # default name again
    assert s.rename(r, "gate")
    dmm = s.row("Keithley 2000 - 1")
    dmm.driver_menu.action_for("lakeshore_335").trigger()
    assert dmm.driver == "lakeshore_335" and dmm.settings["limits"] == (2.0, 300.0)
    dmm.driver_menu.action_for("keithley_2000").trigger()
    assert dmm.settings == {"function": "fres", "nplc": 1}          # back to the file's
    sim = s.row("SR850 - 1")
    sim.driver_menu.action_for("sim_source").trigger()     # no address needed
    assert sim.btn_connect.isEnabled()
    assert s.windowTitle().endswith("*")
    assert s._write(tmp_path / "out.toml")
    text = (tmp_path / "out.toml").read_text("utf-8")
    assert '[devices.gate]' in text and "# CHECK" in text            # comments kept
    assert '[devices."SR850 - 1"]' in text and 'driver = "sim_source"' in text
    cfg = load_station(tmp_path / "out.toml")
    assert cfg.station.shutdown_order[1] == "gate"                  # follows the rename
    assert list(cfg.devices)[:2] == ["gate", "Keithley 2401 - 1"]
    assert cfg.devices["gate"].resource == "GPIB0::26::INSTR"
    assert not s.windowTitle().endswith("*")


def test_open_refuses_a_bad_file_and_keeps_the_table(server, tmp_path):
    s = server()
    (tmp_path / "bad.toml").write_text('[station]\nname = "x"\n[devices."a.b"]\n'
                                       'driver = "sim_source"\n', "utf-8")
    assert not s.load(tmp_path / "bad.toml")
    assert len(s.rows) == 8 and "a.b" in s.log_view.toPlainText()


def test_driver_menu_is_grouped_by_maker(server):
    from gmcore.config.schema import DRIVERS
    from gmcore.gui.server.window import driver_groups
    groups = driver_groups()
    assert list(groups)[:2] == ["Keithley", "Lake Shore"] and "Other" not in groups
    assert sorted(d for items in groups.values() for _, d in items) == sorted(DRIVERS)
    s = server()
    r = s.row("Keithley 2400 - 1")
    menu = r.driver_menu.menu()
    assert [a.text() for a in menu.actions()] == list(groups)                 # level 1
    keithley = menu.actions()[0].menu()
    assert [a.text() for a in keithley.actions()][:4] == [                    # level 2
        "Keithley 2400", "Keithley 2401", "Keithley 2410", "Keithley 2450"]
    assert r.driver_menu.text() == "Keithley 2400"
    assert r.driver_menu.action_for("keithley_2400").isChecked()
    r.driver_menu.action_for("keithley_2410").trigger()
    assert r.driver == "keithley_2410" and r.driver_menu.text() == "Keithley 2410"
