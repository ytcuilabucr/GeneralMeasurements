"""guitestmain.py (driver test bench), offscreen with a fake GPIB scan."""

import importlib.util
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
from PySide6 import QtWidgets  # noqa: E402

from generalmeasurements.devices import FakeTransport, Keithley24xx  # noqa: E402
from generalmeasurements.gui import panels  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "guitestmain", Path(__file__).resolve().parents[1] / "guitestmain.py")
gtm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gtm)

FOUND = [("GPIB0::1::INSTR", "LSCI,MODEL335,LSA1789/#######,1.4"),
         ("GPIB0::3::INSTR", ""),
         ("GPIB0::11::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2000,1234567,A20"),
         ("GPIB0::22::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2401,4321,C30"),
         ("GPIB0::26::INSTR", "KEITHLEY INSTRUMENTS INC.,MODEL 2400,0917134,C24"),
         ("GPIB0::6::INSTR", "Giga-tronics, 2420B, 818003, V4.33b")]


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def bench(app):
    b = gtm.Bench("station.lab.toml", scan=lambda: FOUND)
    t0 = time.monotonic()
    while not b.scanned and time.monotonic() - t0 < 3:
        app.processEvents()
    yield b
    for w in b.windows.values():
        w.panel.timer.stop()
        w.hide()
    b.worker.stop()


def test_match_idn():
    assert gtm.match_idn(FOUND[4][1]) == ("keithley_24xx", "2400")
    assert gtm.match_idn(FOUND[2][1]) == ("keithley_2000", None)
    assert gtm.match_idn("Stanford_Research_Systems,SR850,s/n,ver") == ("sr850", None)
    assert gtm.match_idn("LSCI,MODEL625,LSA179P,1.3/1.1") == ("lakeshore_625", None)
    assert gtm.match_idn(FOUND[5][1])[0] == "gigatronics_2420b"
    assert gtm.match_idn("NOTHING,KNOWN") is None


def test_rows_preset_to_detected_addresses(bench):
    rows = [(r["driver"], r["combo"].currentText()) for r in bench.rows]
    assert ("keithley_24xx", "GPIB0::22::INSTR") in rows        # one row per unit found
    assert ("keithley_24xx", "GPIB0::26::INSTR") in rows
    assert ("keithley_2000", "GPIB0::11::INSTR") in rows
    assert ("lakeshore_335", "GPIB0::1::INSTR") in rows
    assert ("lakeshore_625", "GPIB0::12::INSTR") in rows        # not found: station file
    assert ("sr830", "") in rows                                # unknown anywhere: blank
    assert all(r["open"].isEnabled() == bool(r["combo"].currentText()) for r in bench.rows)


def test_make_cfg_takes_settings_by_address_and_model_from_idn():
    st = gtm.load_station("station.lab.toml")
    name, cfg, src = gtm.make_cfg("keithley_24xx", "GPIB0::22::INSTR", FOUND[3][1], st)
    assert (name, cfg.mode, cfg.model) == ("bias", "curr", "2401") and "[bias]" in src
    name, cfg, src = gtm.make_cfg("keithley_24xx", "GPIB0::5::INSTR",
                                  "KEITHLEY INSTRUMENTS INC.,MODEL 2410,1,A", st)
    assert (name, cfg.model, cfg.resource) == ("gate", "2410", "GPIB0::5::INSTR")
    name, cfg, src = gtm.make_cfg("sr830", "GPIB0::9::INSTR", "", None)
    assert (name, src) == ("lockin", "built-in defaults")


def test_open_shows_window_connects_once_per_address(bench, app, monkeypatch):
    t = FakeTransport({":SOUR:FUNC?": "VOLT", ":OUTP?": "0", ":SOUR:VOLT:LEV?": "0"})

    async def build(name, cfg, others):
        return Keithley24xx(name, t, cfg.model, cfg.mode, cfg.compliance, cfg.limits,
                            cfg.max_rate)
    monkeypatch.setattr(panels, "build_device", build)
    row = next(r for r in bench.rows if r["combo"].currentText() == "GPIB0::26::INSTR")
    w = bench.open_row(row)
    t0 = time.monotonic()
    while w.panel.conn_state != "connected" and time.monotonic() - t0 < 3:
        app.processEvents()
    assert w.panel.conn_state == "connected" and w.isVisible()
    assert w.panel.name == "gate" and "*RST" not in t.log
    assert bench.open_row(row) is w                     # same address: same window
    assert len(bench.windows) == 1
