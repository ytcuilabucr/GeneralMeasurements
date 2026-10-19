"""The legacy driver test bench (gui/legacy/bench.py), offscreen with a fake GPIB scan."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
from PySide6 import QtWidgets  # noqa: E402

from gmcore.config.registry import DRIVER_CLASSES  # noqa: E402
from gmcore.devices import FakeTransport  # noqa: E402
from gmcore.gui.common import panels  # noqa: E402
from gmcore.gui.legacy import bench as gtm  # noqa: E402

FOUND = [("GPIB0::1::INSTR", "LSCI,MODEL335,LSA1789/#######,1.4"),
         ("GPIB0::3::INSTR", ""),
         ("GPIB0::11::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2000,1234567,A20"),
         ("GPIB0::22::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2401,4321,C30"),
         ("GPIB0::26::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2400,0917134,C24"),
         ("GPIB0::6::INSTR", "Giga-tronics, 2420B, 818003, V4.33b")]

# the range queries a Keithley panel makes on connect: "autoranging"
AUTORANGE = {f":{k}:{f}:RANG:AUTO?": "1" for k in ("SOUR", "SENS") for f in ("VOLT", "CURR")}

@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def bench(app):
    b = gtm.Bench("tests/fixtures/lab.toml", scan=lambda: FOUND)
    t0 = time.monotonic()
    while not b.scanned and time.monotonic() - t0 < 3:
        app.processEvents()
    yield b
    for w in b.windows.values():
        w.panel.timer.stop()
        w.hide()
    b.worker.stop()


def test_match_idn():
    assert gtm.match_idn(FOUND[4][1]) == ("keithley_2400", "2400")
    assert gtm.match_idn(FOUND[2][1]) == ("keithley_2000", None)
    assert gtm.match_idn("Stanford_Research_Systems,SR850,s/n,ver") == ("sr850", None)
    assert gtm.match_idn("LSCI,MODEL625,LSA179P,1.3/1.1") == ("lakeshore_625", None)
    assert gtm.match_idn(FOUND[5][1])[0] == "gigatronics_2420b"
    assert gtm.match_idn("NOTHING,KNOWN") is None


def test_rows_preset_to_detected_addresses(bench):
    rows = [(r["driver"], r["combo"].currentText()) for r in bench.rows]
    assert ("keithley_2401", "GPIB0::22::INSTR") in rows        # each model its own row
    assert ("keithley_2400", "GPIB0::26::INSTR") in rows
    assert ("keithley_2000", "GPIB0::11::INSTR") in rows
    assert ("lakeshore_335", "GPIB0::1::INSTR") in rows
    assert ("lakeshore_625", "GPIB0::12::INSTR") in rows        # not found: station file
    assert ("sr830", "") in rows                                # unknown anywhere: blank
    assert all(r["open"].isEnabled() == bool(r["combo"].currentText()) for r in bench.rows)


def test_make_cfg_takes_settings_by_address_and_model_from_idn():
    st = gtm.load_station("tests/fixtures/lab.toml")
    name, cfg, src = gtm.make_cfg("keithley_2401", "GPIB0::22::INSTR", FOUND[3][1], st)
    assert (name, cfg.mode, cfg.driver) == ("bias", "curr", "keithley_2401") and "[bias]" in src
    name, cfg, src = gtm.make_cfg("keithley_2410", "GPIB0::5::INSTR",
                                  "KEITHLEY INSTRUMENTS INC.,MODEL 2410,1,A", st)
    assert (name, cfg.driver, cfg.resource, src) == (    # no 2410 in the station file
        "smu", "keithley_2410", "GPIB0::5::INSTR", "built-in defaults")
    name, cfg, src = gtm.make_cfg("sr830", "GPIB0::9::INSTR", "", None)
    assert (name, src) == ("lockin", "built-in defaults")


def test_every_bench_driver_has_a_panel_and_defaults():
    assert set(gtm.LABELS) == set(gtm.DEFAULTS) == set(gtm.SHORT) <= set(gtm.PANELS)
    for driver in gtm.LABELS:                           # built-in defaults are valid
        name, cfg, src = gtm.make_cfg(driver, "GPIB0::9::INSTR", "", None)
        assert (name, cfg.driver, src) == (gtm.SHORT[driver], driver, "built-in defaults")


def _open_gate(bench, app, monkeypatch):
    """Open the 2400 at GPIB0::26 on a fake transport; -> (window, transport)."""
    t = FakeTransport({**AUTORANGE, ":SOUR:FUNC?": "VOLT", ":OUTP?": "0", ":SOUR:VOLT:LEV?": "0"})

    async def build(name, cfg, others):
        return DRIVER_CLASSES[cfg.driver](name, t, cfg.mode, cfg.compliance, cfg.limits)
    monkeypatch.setattr(panels, "build_device", build)
    row = next(r for r in bench.rows if r["combo"].currentText() == "GPIB0::26::INSTR")
    w = bench.open_row(row)
    t0 = time.monotonic()
    while not (w.panel.conn_state == "connected" and w.panel.folds) \
            and time.monotonic() - t0 < 3:
        app.processEvents()
    app.processEvents()             # rows built in a shown window appear on the next pass
    return w, t, row


def test_open_shows_window_connects_once_per_address(bench, app, monkeypatch):
    w, t, row = _open_gate(bench, app, monkeypatch)
    assert w.panel.conn_state == "connected" and w.isVisible()
    assert w.panel.name == "gate" and "*RST" not in t.log
    assert bench.open_row(row) is w                     # same address: same window
    assert len(bench.windows) == 1


def test_device_window_switches_detail_level(bench, app, monkeypatch):
    w, _, _ = _open_gate(bench, app, monkeypatch)
    p = w.panel
    assert isinstance(p, panels.DefinitionPanel) and len(p.folds) == 2
    assert w.detail_actions[0].isChecked() and p.detail == 0
    shown = {pid: r.now.isVisibleTo(p) for pid, r in p.rows.items()}
    assert shown == {"output": True, "level": True, "compliance": False, "nplc": False,
                     "source_range": False, "sense_range": False,
                     "effective_compliance": False}
    w.detail_actions[1].trigger()
    assert p.detail == 1 and p.rows["compliance"].now.isVisibleTo(p)
    assert not p.rows["nplc"].now.isVisibleTo(p)
    w.detail_actions[2].trigger()
    assert p.detail == 2 and p.rows["nplc"].now.isVisibleTo(p)
    w.detail_actions[0].trigger()
    assert p.detail == 0 and not any(f.is_open() for f in p.folds)


def test_cryostat_window_has_no_level_buttons(bench):
    st = gtm.load_station("tests/fixtures/lab.toml")
    name, cfg, src = gtm.make_cfg("lakeshore_335", "GPIB0::1::INSTR", FOUND[0][1], st)
    w = gtm.DeviceWindow(gtm.PANELS["lakeshore_335"](name, cfg, bench), bench, src)
    assert w.detail_actions == {} and not w.findChildren(QtWidgets.QToolBar)
    w.deleteLater()
