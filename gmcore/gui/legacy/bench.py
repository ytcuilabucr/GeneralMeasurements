"""Driver test bench: open any one instrument in its own window.

    python -m gmcore.gui.legacy.bench [--station "Instrument config/lab.toml"]

Superseded by the Instrument Server (gui/server), which keeps the instrument
list in a config instead of scanning; kept while drivers are being brought up.

Lists every driver that has a GUI panel, each with a GPIB address. At start
(and on Rescan) every GPIB address is asked *IDN?, and each row is preset to
the address where its instrument answered; a type found twice (two Keithley
2400) gets a row per unit. Open builds the device and shows its panel in a new
window, connected. Connecting only queries (no reset, no writes).

Settings a driver needs (limits, ramp rates, compliance) come from the station
file: the entry at the chosen address, else the first entry with that driver,
else the conservative defaults below. The window owns that instrument while it
is open (single-owner rule): don't run a scan on it at the same time.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

from pydantic import TypeAdapter

# The config (pydantic models) before PySide6: once PySide6 is imported, its import
# hook inspects new modules and trips over pydantic's lazy imports.
from ...config import load_station
from ...config.schema import DeviceCfg, StationConfig
from PySide6 import QtCore, QtGui, QtWidgets
from ..common import icons, theme
from ..common.panels import Panel, detail_actions
from ..common.registry import PANELS
from ..common.worker import AsyncWorker

KEITHLEY_SMUS = ("2400", "2401", "2410", "2450")
LABELS = {
    **{f"keithley_{m}": f"Keithley {m} SourceMeter" for m in KEITHLEY_SMUS},
    "keithley_2000": "Keithley 2000 multimeter",
    "sr830": "SRS SR830 lock-in",
    "sr850": "SRS SR850 lock-in",
    "lakeshore_335": "Lake Shore 335 temperature controller",
    "lakeshore_625": "Lake Shore 625 magnet supply",
    "gigatronics_2420b": "Giga-tronics 2420B signal generator",
    "lm510": "Cryomagnetics LM-510 helium level meter",
}
# *IDN? pattern -> driver ("{}": the matched model number)
IDN_PATTERNS = [
    (re.compile(r"MODEL 2000\b", re.I), "keithley_2000"),
    (re.compile(rf"MODEL ({'|'.join(KEITHLEY_SMUS)})\b", re.I), "keithley_{}"),
    (re.compile(r"MODEL ?335\b", re.I), "lakeshore_335"),
    (re.compile(r"MODEL ?625\b", re.I), "lakeshore_625"),
    (re.compile(r"\bSR850\b", re.I), "sr850"),
    (re.compile(r"\bSR830\b", re.I), "sr830"),
    (re.compile(r"giga-?tronics.*2420|\b2420B\b", re.I), "gigatronics_2420b"),
    (re.compile(r"\bLM-?510\b", re.I), "lm510"),
]
# when the station file has no entry for a driver: small limits, slow rates
DEFAULTS: dict[str, dict] = {
    **{f"keithley_{m}": {"mode": "volt", "compliance": 1e-6, "limits": (-1.0, 1.0)}
       for m in KEITHLEY_SMUS},
    "keithley_2000": {},
    "sr830": {},
    "sr850": {},
    "lakeshore_335": {"limits": (2.0, 300.0), "max_rate": 1 / 60},
    "lakeshore_625": {"limits": (-1.0, 1.0), "max_rate": 0.01},
    "gigatronics_2420b": {"limits": (1e9, 2e10), "power_dbm": -20.0, "max_power_dbm": 0.0},
    "lm510": {},
}
SHORT = {**{f"keithley_{m}": "smu" for m in KEITHLEY_SMUS}, "keithley_2000": "dmm",
         "sr830": "lockin",
         "sr850": "lockin", "lakeshore_335": "temp", "lakeshore_625": "magnet",
         "gigatronics_2420b": "rf", "lm510": "helium"}
_COLORS = {"info": "", "warn": "#b9770e", "error": "#c0392b"}
_CFG = TypeAdapter(DeviceCfg)


def match_idn(idn: str) -> tuple[str, str | None] | None:
    """*IDN? reply -> (driver, model or None), or None if no driver fits."""
    for pat, driver in IDN_PATTERNS:
        m = pat.search(idn)
        if m:
            model = m.group(1) if "{}" in driver else None
            return driver.format(model), model
    return None


def scan_gpib(timeout_ms: int = 1500) -> list[tuple[str, str]]:
    """(resource, *IDN? reply or '') for every GPIB instrument. Queries only."""
    import pyvisa
    rm = pyvisa.ResourceManager()
    found = []
    for res in rm.list_resources("GPIB?*::INSTR"):
        idn = ""
        try:
            inst = rm.open_resource(res, timeout=timeout_ms)
            try:
                inst.write_termination = "\n"
                idn = inst.query("*IDN?").strip()
            except pyvisa.VisaIOError:
                try:
                    inst.clear()                   # don't leave a late reply behind
                except pyvisa.VisaIOError:
                    pass
            finally:
                inst.close()
        except pyvisa.VisaIOError:
            pass
        found.append((str(res), idn))
    return found


def make_cfg(driver: str, resource: str, idn: str, station: StationConfig | None,
             station_name: str = "station") -> tuple[str, DeviceCfg, str]:
    """-> (role name, validated config, where the settings came from)."""
    entries = [(n, d) for n, d in (station.devices.items() if station else [])
               if d.driver == driver]
    entry = next(((n, d) for n, d in entries if getattr(d, "resource", "") == resource),
                 entries[0] if entries else None)
    if entry:
        name, d = entry
        data = d.model_dump(exclude={"window"})
        source = f"{station_name} [{name}]"
    else:
        name, data, source = SHORT[driver], dict(DEFAULTS[driver]), "built-in defaults"
    data.update(driver=driver, resource=resource)
    return name, _CFG.validate_python(data), source


class DeviceWindow(QtWidgets.QMainWindow):
    """One instrument's panel. Closing disconnects (asks: leave as is / safe state)."""

    closed = QtCore.Signal(str)

    def __init__(self, panel: Panel, bench: Bench, source: str):
        super().__init__()
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.panel, self.bench = panel, bench
        self.resource = panel.cfg.resource
        self.setWindowTitle(f"{panel.name} — {LABELS[panel.cfg.driver]} — {self.resource}")
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        self.setCentralWidget(scroll)
        self.statusBar().showMessage(f"settings from {source}")
        self.resize(780, 860)
        self.detail_actions = {}                        # cryostat panels: nothing to fold
        if panel.FOLDS:
            self.detail_actions = detail_actions(self, self.addToolBar("Show"), [panel],
                                                 panel.set_detail)

    def closeEvent(self, event) -> None:
        p = self.panel
        if p.dev is not None:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Close")
            box.setText(f"{p.name} is connected.")
            box.setInformativeText("Leave it in its present state, or put it in its safe "
                                   "state first?")
            leave = box.addButton("Leave as is", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            safe = box.addButton("Safe state, then close",
                                 QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked not in (leave, safe):
                event.ignore()
                return
            p.timer.stop()
            p.stop_move()
            if clicked is safe:
                try:
                    self.bench.worker.run(p.dev.safe_state(), 120)
                    self.bench.log("info", f"{p.name}: safe state: done")
                except Exception as e:  # noqa: BLE001
                    QtWidgets.QMessageBox.warning(self, "Safe state", f"{type(e).__name__}: {e}")
            p.disconnect_dev()
        self.closed.emit(self.resource)
        event.accept()


class Bench(QtWidgets.QMainWindow):
    """Host for the panels: worker, open devices and log (what panels expect of a station)."""

    def __init__(self, station: str | Path | None = None, scan=scan_gpib):
        super().__init__()
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.worker = AsyncWorker()
        self.devices: dict[str, object] = {}
        self.windows: dict[str, DeviceWindow] = {}     # by resource
        self._scan = scan
        self.found: list[tuple[str, str]] = []
        self.scanned = False
        self.station_name = Path(station).name if station else "station"
        self.station_cfg = load_station(station) if station and Path(station).exists() else None
        self.setWindowTitle("GeneralMeasurements — driver test bench")
        self.resize(980, 560)

        bar = self.addToolBar("Bench")
        self.act_scan = QtGui.QAction("Rescan GPIB", self)
        icons.set_icon(self.act_scan, "read")
        self.act_scan.setToolTip("Ask *IDN? at every GPIB address (queries only)")
        self.act_scan.triggered.connect(self.rescan)
        bar.addAction(self.act_scan)
        theme.add_menu(self)

        self.table = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.table)
        self.grid.setColumnStretch(2, 1)
        top = QtWidgets.QScrollArea()
        top.setWidgetResizable(True)
        top.setWidget(self.table)
        self.log_view = QtWidgets.QTextEdit(readOnly=True)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.log_view)
        split.setSizes([360, 160])
        self.setCentralWidget(split)
        self.rows: list[dict] = []
        self._build_rows()
        if self.station_cfg is None:
            self.log("warn", f"no station file ({station}); using built-in defaults")
        self.rescan()

    # -- log ---------------------------------------------------------------------------
    def log(self, level: str, text: str) -> None:
        color = _COLORS.get(level, "")
        line = f"{time.strftime('%H:%M:%S')}  {text}".replace("&", "&amp;").replace("<", "&lt;")
        self.log_view.append(f"<span style='color:{color}'>{line}</span>" if color else line)
        self.statusBar().showMessage(text, 8000)
        name = text.split(":", 1)[0]
        for w in self.windows.values():
            if w.panel.name == name:
                w.statusBar().showMessage(text, 8000)

    # -- rows ----------------------------------------------------------------------------
    def _build_rows(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.rows.clear()
        for col, head in enumerate(("Driver", "GPIB address", "Instrument (*IDN?)",
                                    "Settings from", "")):
            self.grid.addWidget(QtWidgets.QLabel(f"<b>{head}</b>"), 0, col)
        hits: dict[str, list[str]] = {}
        for res, idn in self.found:
            m = match_idn(idn) if idn else None
            if m:
                hits.setdefault(m[0], []).append(res)
        for driver in LABELS:
            if driver not in PANELS:
                continue
            for preset in hits.get(driver) or [None]:
                self._add_row(driver, preset)
        self.grid.setRowStretch(len(self.rows) + 1, 1)

    def _add_row(self, driver: str, preset: str | None) -> None:
        r = len(self.rows) + 1
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        combo.setMinimumWidth(170)
        for res, idn in self.found:
            combo.addItem(res)
            combo.setItemData(combo.count() - 1, idn or "no reply",
                              QtCore.Qt.ItemDataRole.ToolTipRole)
        if preset is None:          # not detected: the station file's address, if any
            preset = next((d.resource for d in (self.station_cfg.devices.values()
                                                if self.station_cfg else [])
                           if d.driver == driver and hasattr(d, "resource")), "")
        combo.setCurrentText(preset)
        idn_label = QtWidgets.QLabel()
        idn_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        src_label = QtWidgets.QLabel()
        btn = QtWidgets.QPushButton("Open")
        icons.set_icon(btn, "open_panel")
        row = {"driver": driver, "combo": combo, "idn": idn_label, "src": src_label,
               "open": btn}
        combo.currentTextChanged.connect(lambda _t, row=row: self._refresh(row))
        btn.clicked.connect(lambda _=False, row=row: self.open_row(row))
        for col, w in enumerate((QtWidgets.QLabel(LABELS[driver]), combo, idn_label,
                                 src_label, btn)):
            self.grid.addWidget(w, r, col)
        self.rows.append(row)
        self._refresh(row)

    def _idn_of(self, res: str) -> str:
        return next((idn for r, idn in self.found if r == res), "")

    def _refresh(self, row: dict) -> None:
        res = row["combo"].currentText().strip()
        idn = self._idn_of(res)
        m = match_idn(idn) if idn else None
        if not res:
            text = "<span style='color:#888'>not detected; type an address</span>"
        elif not res.upper().startswith("GPIB"):
            text = "<span style='color:#888'>not scanned (only GPIB is scanned)</span>"
        elif not idn:
            text = "<span style='color:#888'>not found or no reply</span>" if self.scanned \
                else "<span style='color:#888'>scanning…</span>"
        elif m and m[0] == row["driver"]:
            text = f"<span style='color:#1e8449'>{idn[:70]}</span>"
        else:
            text = f"<span style='color:#c0392b'>{idn[:60]} (other driver)</span>"
        row["idn"].setText(text)
        row["idn"].setToolTip(idn)
        try:
            row["src"].setText(make_cfg(row["driver"], res or "GPIB0::0::INSTR", idn,
                                        self.station_cfg, self.station_name)[2])
        except Exception as e:  # noqa: BLE001
            row["src"].setText(f"invalid: {e}")
        row["open"].setEnabled(bool(res) and self.act_scan.isEnabled())

    # -- scan ----------------------------------------------------------------------------
    def rescan(self) -> None:
        if self.windows:
            self.log("warn", "close the open instrument windows before rescanning "
                             "(single owner)")
            return
        self.act_scan.setEnabled(False)
        for row in self.rows:
            row["open"].setEnabled(False)
        self.log("info", "scanning GPIB (*IDN? at each address)…")
        t0 = time.monotonic()

        def done(found):
            self.found, self.scanned = found, True
            self.act_scan.setEnabled(True)
            self._build_rows()
            known = sum(1 for _, idn in found if idn and match_idn(idn))
            self.log("info", f"scan: {len(found)} GPIB addresses, {known} with a driver "
                             f"({time.monotonic() - t0:.1f} s)")
            for res, idn in found:
                self.log("info", f"  {res}: {idn or 'no reply'}")

        def failed(e):
            self.scanned = True
            self.act_scan.setEnabled(True)
            self._build_rows()
            self.log("error", f"scan: {type(e).__name__}: {e}")
        self.worker.submit(asyncio.to_thread(self._scan), done, failed)

    # -- open ----------------------------------------------------------------------------
    def open_row(self, row: dict) -> DeviceWindow | None:
        res = row["combo"].currentText().strip()
        if res in self.windows:                        # one session per instrument
            w = self.windows[res]
            w.show()
            w.raise_()
            w.activateWindow()
            return w
        driver = row["driver"]
        idn = self._idn_of(res)
        m = match_idn(idn) if idn else None
        if idn and (m is None or m[0] != driver):
            r = QtWidgets.QMessageBox.question(
                self, "Open", f"{res} answers\n{idn}\nwhich doesn't look like a "
                f"{LABELS[driver]}. Open it with the {driver} driver anyway?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return None
        try:
            name, cfg, source = make_cfg(driver, res, idn, self.station_cfg, self.station_name)
        except Exception as e:  # noqa: BLE001
            self.log("error", f"{driver}: {e}")
            return None
        base, n = name, 2
        while name in self.devices or any(w.panel.name == name for w in self.windows.values()):
            name, n = f"{base}{n}", n + 1
        panel = PANELS[driver](name, cfg, self)
        w = DeviceWindow(panel, self, source)
        w.closed.connect(self._closed)
        self.windows[res] = w
        n_open = len(self.windows)
        w.move(self.x() + 60 + 30 * n_open, self.y() + 30 * n_open)
        w.show()
        self.log("info", f"{name}: {driver} at {res}, settings from {source}")
        panel.connect_dev()
        return w

    def _closed(self, res: str) -> None:
        self.windows.pop(res, None)

    def closeEvent(self, event) -> None:
        for w in list(self.windows.values()):
            if not w.close():                          # the operator cancelled
                event.ignore()
                return
        self.worker.stop()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--station", default=str(Path("Instrument config") / "lab.toml"),
                    help="instrument config to take driver settings from")
    args = ap.parse_args(argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GeneralMeasurements")
    bench = Bench(args.station)
    bench.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
