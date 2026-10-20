"""Instrument Server: connects to every instrument in an instrument config.

    python instrumentserver.py ["Instrument config/lab.toml"]
    gm server ["Instrument config/lab.toml"]

One row per instrument in the config (`Instrument config/*.toml`, the station
schema): address | name | driver | connection light | Connect / Disconnect |
Open panel. The name is the instrument's working name: sequences and data
keys use it (`<name>.v`), so it can be changed only while disconnected.
Connecting only queries the instrument (no reset, no writes), as in every
panel. Open panel shows the driver's dedicated panel, or the standard one.

The server owns the sessions (single-owner rule). General Measurement, the
sequence editor, will run in the same program and use `server.devices`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError
from PySide6 import QtCore, QtGui, QtWidgets

from ...config.schema import DRIVER_DEFAULTS, DRIVERS, StationConfig, check_name
from ...engine.runner import safe_shutdown
from ..common.panels import Panel, detail_actions
from ..common.registry import panel_for
from ..common import appsettings, icons, theme
from ..common.appsettings import CONFIG_DIR
from ..common.form_types import PanelWindowForm, ServerForm
from ..common.uiload import load_ui
from ..common.widgets import Led, MenuPicker
from ..common.worker import AsyncWorker
from .quick import QuickView

FILE_FILTER = "Instrument config (*.toml);;All files (*)"
LAST_KEY, OPEN_LAST_KEY = "instrument_server/last_config", "instrument_server/open_last"
# driver -> model name for default instrument names ("Keithley 2400 - 1")
MODEL_NAMES = {
    "keithley_2400": "Keithley 2400", "keithley_2401": "Keithley 2401",
    "keithley_2410": "Keithley 2410", "keithley_2450": "Keithley 2450",
    "keithley_2000": "Keithley 2000", "sr830": "SR830", "sr850": "SR850",
    "lakeshore_335": "Lake Shore 335", "lakeshore_625": "Lake Shore 625",
    "gigatronics_2420b": "Giga-tronics 2420B", "lm510": "LM-510", "aotf_octal": "AOTF",
    "ysl_aotf": "YSL AOTF", "ysl_sc_pro8": "SC-PRO8",
    "sim_source": "Sim source", "sim_magnet": "Sim magnet", "sim_lockin": "Sim lock-in",
}
_COLORS = {"info": "", "warn": "#b9770e", "error": "#c0392b"}
_GREEN, _RED = "#1e8449", "#c0392b"


# The Driver column's drop-down: manufacturer -> drivers. A driver missing here
# is listed under "Other", so a new one shows up before it is filed.
DRIVER_GROUPS = {
    "Keithley": ("keithley_2400", "keithley_2401", "keithley_2410", "keithley_2450",
                 "keithley_2000"),
    "Lake Shore": ("lakeshore_335", "lakeshore_625"),
    "Stanford Research": ("sr830", "sr850"),
    "Giga-tronics": ("gigatronics_2420b",),
    "Crystal Technology (Fianium)": ("aotf_octal",),
    "YSL Photonics": ("ysl_sc_pro8", "ysl_aotf"),
    "Cryomagnetics": ("lm510",),
    "Simulated": ("sim_source", "sim_magnet", "sim_lockin"),
}


def driver_groups() -> dict[str, list[tuple[str, str]]]:
    """{group: [(label, driver)]} for the Driver drop-down, from DRIVERS."""
    filed = {d for ds in DRIVER_GROUPS.values() for d in ds}
    groups = {g: [(MODEL_NAMES.get(d, d), d) for d in ds if d in DRIVERS]
              for g, ds in DRIVER_GROUPS.items()}
    groups["Other"] = [(d, d) for d in DRIVERS if d not in filed]
    return {g: items for g, items in groups.items() if items}


def model_name(driver: str, settings: dict[str, Any]) -> str:
    return MODEL_NAMES.get(driver, driver)


def default_name(model: str, taken: set[str]) -> str:
    """'<model> - <n>' with the lowest n not taken."""
    n = 1
    while f"{model} - {n}" in taken:
        n += 1
    return f"{model} - {n}"


class InstrumentRow:
    """One instrument: its config entry, its widgets, and (once connected or
    opened) its panel."""

    # the table's widgets for this row, made by InstrumentServer._add_row_widgets
    address: QtWidgets.QLabel
    name_edit: QtWidgets.QLineEdit
    driver_menu: MenuPicker
    led: Led
    btn_connect: QtWidgets.QPushButton
    btn_safe: QtWidgets.QPushButton
    btn_open: QtWidgets.QPushButton

    def __init__(self, name: str, data: dict[str, Any]):
        self.name = self.saved_name = name           # saved_name: its key in the file
        self.driver = self.saved_driver = data["driver"]
        self.resource: str | None = data.get("resource")
        self.saved_settings = {k: v for k, v in data.items()
                               if k not in ("driver", "resource")}
        self.settings = dict(self.saved_settings)
        self.panel: Panel | None = None
        self.window: PanelWindow | None = None
        self.state = "disconnected"

    def cfg(self):
        """The validated config for the current driver (raises ValidationError)."""
        data: dict[str, Any] = {"driver": self.driver, **self.settings}
        if "resource" in DRIVERS[self.driver].model_fields:
            data["resource"] = self.resource
        return DRIVERS[self.driver].model_validate(data)

    @property
    def connected(self) -> bool:
        return self.panel is not None and self.panel.dev is not None

    @property
    def busy(self) -> bool:
        """Connected or connecting: the name and driver are fixed."""
        return self.connected or self.state == "connecting…"


class PanelWindow(QtWidgets.QMainWindow, PanelWindowForm):
    """One instrument's panel (panel_window.ui). Closing it only hides it: the
    connection belongs to the server's table."""

    def __init__(self, panel: Panel, address: str):
        super().__init__()
        load_ui("panel_window", self)
        self.panel = panel
        self.setWindowTitle(f"{panel.name} — {panel.cfg.driver} — {address}")
        icons.set_window_icon(self, icons.driver_role(panel.cfg.driver))
        self.scroll_area.setWidget(panel)
        self.detail_actions: dict[int, QtGui.QAction] = {}
        if panel.FOLDS:
            self.detail_actions = detail_actions(self, self.show_bar, [panel],
                                                 panel.set_detail)
        else:
            self.show_bar.hide()


class InstrumentServer(QtWidgets.QMainWindow, ServerForm):
    """The table of instruments, the File menu and the log (server.ui). Panels use
    it as their station: `log`, `worker` and `devices`."""

    instruments_changed = QtCore.Signal()      # an instrument connected or disconnected
    menuMiscellaneous: QtWidgets.QMenu         # added by theme.add_menu

    def __init__(self, path: str | Path | None = None,
                 settings: appsettings.MiscSettings | QtCore.QSettings | None = None):
        super().__init__()
        # remembered between sessions: the last config, the quick view's hidden
        # readings (Instrument config/misc.toml, shared with the theme)
        self.settings = settings if settings is not None else appsettings.misc()
        self.worker = AsyncWorker()
        self.devices: dict[str, object] = {}
        self.rows: list[InstrumentRow] = []
        self.path: Path | None = None
        self.doc: tomlkit.TOMLDocument = tomlkit.document()
        self.station_name = "instruments"
        self._dirty = False
        self._row_widgets: list[QtWidgets.QWidget] = []
        self.measurement: QtWidgets.QMainWindow | None = None
        self._pending: list[InstrumentRow] = []    # Connect all: waiting for their source

        load_ui("server", self)
        icons.set_window_icon(self, "app")
        self.actionOpen.triggered.connect(self.open_dialog)
        self.actionSave.triggered.connect(self.save)
        self.actionSaveAs.triggered.connect(self.save_as)
        self.actionExit.triggered.connect(self.close)
        self.actionGeneralMeasurement.triggered.connect(self.open_measurement)
        self.split.setSizes([420, 160])
        self.quick = QuickView(self.quick_blocks, self.quick_empty, self.settings)
        self.btn_quick_settings.clicked.connect(lambda: self.quick.edit(self))
        self.btn_connect_all.clicked.connect(self.connect_all)
        self.btn_safe_all.clicked.connect(self.safe_all)
        self.btn_disconnect_all.clicked.connect(self.disconnect_all)
        theme.add_menu(self)                 # only here: the panels follow it
        self.menuMiscellaneous.addSeparator()
        self.actionOpenLast = self.menuMiscellaneous.addAction(
            "Open the last instrument config at start")
        self.actionOpenLast.setObjectName("actionOpenLast")
        self.actionOpenLast.setCheckable(True)
        self.actionOpenLast.setChecked(open_last(self.settings))
        self.actionOpenLast.toggled.connect(
            lambda on: self.settings.setValue(OPEN_LAST_KEY, on))
        if getattr(self.settings, "error", ""):
            self.log("warn", self.settings.error)

        if path is not None:
            self.load(path)
        else:
            self._build_table()
            self._update_title()

    # -- log -----------------------------------------------------------------------------
    def log(self, level: str, text: str) -> None:
        color = _COLORS.get(level, "")
        line = f"{time.strftime('%H:%M:%S')}  {text}".replace("&", "&amp;").replace("<", "&lt;")
        self.log_view.append(f"<span style='color:{color}'>{line}</span>" if color else line)
        self.statusBar().showMessage(text, 8000)

    # -- the config file ---------------------------------------------------------------
    def load(self, path: str | Path) -> bool:
        path = Path(path)
        try:
            text = path.read_text("utf-8")
            StationConfig.model_validate(tomlkit.parse(text).unwrap())
        except (OSError, ValueError, TOMLKitError) as e:
            self.log("error", f"open {path}: {e}")
            return False
        self._drop_panels()
        self.doc = tomlkit.parse(text)
        raw = self.doc.unwrap()
        self.path = path
        self.station_name = raw["station"]["name"]
        self.rows = [InstrumentRow(n, d) for n, d in raw["devices"].items()]
        self._dirty = False
        self._build_table()
        self._update_title()
        self.settings.setValue(LAST_KEY, appsettings.stored_path(path))
        self.log("info", f"opened {path} ({len(self.rows)} instruments)")
        return True

    def to_toml(self) -> str:
        """The config as it is in the table, keeping the file's comments and layout
        for every entry whose driver didn't change."""
        doc = tomlkit.parse(tomlkit.dumps(self.doc)) if self.doc else tomlkit.document()
        if "station" not in doc:
            doc["station"] = {"name": self.station_name, "shutdown_order": []}
        renamed = {r.saved_name: r.name for r in self.rows}
        order = doc["station"].get("shutdown_order")
        if order is not None:
            for i, n in enumerate(list(order)):
                order[i] = renamed.get(n, n)
        old = doc.get("devices", {})
        devices = tomlkit.table(is_super_table=True)
        for r in self.rows:
            if r.saved_name in old and r.driver == r.saved_driver:
                devices[r.name] = old[r.saved_name]
            else:
                t = tomlkit.table()
                t["driver"] = r.driver
                for k, v in r.cfg().model_dump(exclude_defaults=True).items():
                    if k != "driver":
                        t[k] = list(v) if isinstance(v, tuple) else v
                devices[r.name] = t
        doc["devices"] = devices
        return tomlkit.dumps(doc)

    def save(self) -> bool:
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        start = str(self.path or CONFIG_DIR)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save instrument config", start,
                                                        FILE_FILTER)
        return bool(path) and self._write(Path(path))

    def _write(self, path: Path) -> bool:
        try:
            text = self.to_toml()
            StationConfig.model_validate(tomlkit.parse(text).unwrap())
            path.write_text(text, "utf-8")
        except (OSError, ValueError) as e:
            self.log("error", f"save {path}: {e}")
            return False
        self.doc, self.path = tomlkit.parse(text), path
        for r in self.rows:
            r.saved_name, r.saved_driver = r.name, r.driver
            r.saved_settings = dict(r.settings)
        self._dirty = False
        self._update_title()
        self.settings.setValue(LAST_KEY, appsettings.stored_path(path))
        self.log("info", f"saved {path}")
        return True

    def open_dialog(self) -> None:
        if any(r.busy for r in self.rows):
            self.log("warn", "disconnect every instrument before opening another config "
                             "(single owner)")
            return
        if not self._maybe_save():
            return
        start = str(self.path.parent if self.path else CONFIG_DIR)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open instrument config", start,
                                                        FILE_FILTER)
        if path:
            self.load(path)

    def _maybe_save(self) -> bool:
        """Unsaved changes: save, discard, or cancel (False)."""
        if not self._dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", "Save the changes to the instrument config?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel)
        if r == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save()
        return r == QtWidgets.QMessageBox.StandardButton.Discard

    def _set_dirty(self) -> None:
        self._dirty = True
        self._update_title()

    def _update_title(self) -> None:
        where = str(self.path) if self.path else "no file"
        self.setWindowTitle(f"Instrument Server — {where}{' *' if self._dirty else ''}")

    # -- table ---------------------------------------------------------------------------
    def _build_table(self) -> None:
        """One grid row per instrument, under the form's header row."""
        for w in self._row_widgets:
            self.grid.removeWidget(w)
            w.deleteLater()
        self._row_widgets = []
        for i in range(self.grid.rowCount()):
            self.grid.setRowStretch(i, 0)
        for i, r in enumerate(self.rows, start=1):
            self._add_row_widgets(i, r)
        self.grid.setRowStretch(len(self.rows) + 1, 1)
        self._update_all_buttons()

    def _add_row_widgets(self, i: int, r: InstrumentRow) -> None:
        r.address = QtWidgets.QLabel(r.resource or "simulated")
        r.address.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        r.name_edit = QtWidgets.QLineEdit(r.name)
        r.name_edit.setToolTip("The working name: sequences and data use it. Clear it for "
                               "the default name.")
        r.name_edit.editingFinished.connect(lambda r=r: self.rename(r, r.name_edit.text()))
        r.driver_menu = MenuPicker(driver_groups())
        r.driver_menu.set_current_data(r.driver)
        r.driver_menu.picked.connect(lambda d, r=r: self.set_driver(r, d))
        r.led = Led("")
        r.btn_connect = QtWidgets.QPushButton()
        r.btn_connect.setMinimumWidth(100)
        r.btn_connect.clicked.connect(lambda _=False, r=r: self.toggle_connection(r))
        r.btn_safe = QtWidgets.QPushButton("Safe state")
        icons.set_icon(r.btn_safe, "safe")
        r.btn_safe.clicked.connect(lambda _=False, r=r: self.safe(r))
        r.btn_open = QtWidgets.QPushButton("Open panel")
        icons.set_icon(r.btn_open, "open_panel")
        r.btn_open.clicked.connect(lambda _=False, r=r: self.open_panel(r))
        for col, w in enumerate((r.address, r.name_edit, r.driver_menu, r.led, r.btn_connect,
                                 r.btn_safe, r.btn_open)):
            self.grid.addWidget(w, i, col)
            self._row_widgets.append(w)
        self._update_row(r)

    def _update_row(self, r: InstrumentRow) -> None:
        busy = r.busy
        try:
            r.cfg()
            problem = ""
        except ValidationError as e:
            problem = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'config'}: {x['msg']}"
                                for x in e.errors())
        if r.connected:
            r.led.set("on" if r.state == "connected" else "warn", r.state)
        else:
            r.led.set({"connecting…": "busy", "error": "bad"}.get(r.state, "off"), r.state)
        r.led.setToolTip(r.panel.status.toolTip() if r.panel else "")
        on = r.connected
        r.btn_connect.setText("Disconnect" if busy else "Connect")
        icons.set_icon(r.btn_connect, "disconnect" if busy else "connect", "white")
        r.btn_connect.setStyleSheet(f"background: {_RED if busy else _GREEN}; color: white; "
                                    "font-weight: bold; padding: 3px 10px;")
        r.btn_connect.setEnabled(on or (not busy and not problem))
        r.btn_connect.setToolTip(problem or ("Close the session; the instrument keeps its "
                                             "present state" if busy else
                                             "Open the session and read the present state "
                                             "(no reset, no writes)"))
        r.btn_safe.setEnabled(on)
        r.btn_safe.setToolTip(r.panel.safe_tip() if on and r.panel else
                              "Connect first")
        r.btn_open.setEnabled(not problem)
        r.name_edit.setEnabled(not busy)
        r.driver_menu.setEnabled(not busy)
        r.driver_menu.setToolTip(problem or f"The driver for this instrument ({r.driver})")
        self._update_all_buttons()

    def _can_connect(self, r: InstrumentRow) -> bool:
        if r.busy:
            return False
        try:
            r.cfg()
        except ValidationError:
            return False
        return True

    def _update_all_buttons(self) -> None:
        if not hasattr(self, "btn_connect_all"):          # still loading the form
            return
        self.btn_connect_all.setEnabled(any(self._can_connect(r) for r in self.rows))
        live = any(r.connected for r in self.rows)
        self.btn_safe_all.setEnabled(live)
        self.btn_disconnect_all.setEnabled(any(r.busy for r in self.rows) or bool(self._pending))

    def row(self, name: str) -> InstrumentRow:
        return next(r for r in self.rows if r.name == name)

    # -- name and driver -----------------------------------------------------------------
    def rename(self, r: InstrumentRow, new: str) -> bool:
        new = new.strip()
        if r.busy or new == r.name:
            r.name_edit.setText(r.name)
            return new == r.name
        taken = {x.name for x in self.rows if x is not r}
        if not new:
            new = default_name(model_name(r.driver, r.settings), taken)
        try:
            check_name(new)
            if new in taken:
                raise ValueError(f"name {new!r} is already used")
        except ValueError as e:
            self.log("error", f"rename {r.name}: {e}")
            r.name_edit.setText(r.name)
            return False
        self.log("info", f"{r.name} renamed to {new}")
        self._drop_panel(r)
        r.name = new
        r.name_edit.setText(new)
        self._set_dirty()
        return True

    def set_driver(self, r: InstrumentRow, driver: str) -> None:
        if driver == r.driver:
            return
        if r.busy:
            r.driver_menu.set_current_data(r.driver)
            return
        r.driver = driver
        if driver == r.saved_driver:
            r.settings = dict(r.saved_settings)
        else:
            r.settings = dict(DRIVER_DEFAULTS.get(driver, {}))
            what = ("built-in defaults (small limits, slow rates)" if r.settings
                    else "the driver's default settings")
            self.log("warn", f"{r.name}: driver {driver}, with {what}; check them before "
                             "connecting")
        self._drop_panel(r)
        self._set_dirty()
        self._update_row(r)

    # -- connection and panels -----------------------------------------------------------
    def _panel(self, r: InstrumentRow) -> Panel:
        """The row's panel, made on first use; it holds the connection."""
        if r.panel is None:
            r.panel = panel_for(r.driver)(r.name, r.cfg(), self)
            r.panel.status_changed.connect(lambda _n, state, r=r: self._on_status(r, state))
            r.panel.reading.connect(self.quick.update)
            r.panel.connected.connect(lambda _n: self._refresh_quick())   # (re)attached
            r.panel.connected.connect(lambda _n: self._connect_pending())
        return r.panel

    def _on_status(self, r: InstrumentRow, state: str) -> None:
        r.state = state
        self._update_row(r)
        if state in ("connected", "disconnected"):
            self._refresh_quick()
            self.instruments_changed.emit()

    def shutdown_order(self) -> list[str]:
        """The config's [station] shutdown_order (names of the table's instruments)."""
        return list(self.doc.unwrap().get("station", {}).get("shutdown_order", []))

    def _refresh_quick(self) -> None:
        """The quick view's blocks: every connected instrument's readings."""
        self.quick.set_instruments({r.name: r.panel.dev.describe() for r in self.rows
                                    if r.connected and r.panel is not None})

    def toggle_connection(self, r: InstrumentRow) -> None:
        if r.busy:
            self.disconnect(r)
        else:
            self.connect(r)

    def connect(self, r: InstrumentRow) -> None:
        self.log("info", f"{r.name}: connecting to {r.resource or 'simulator'} ({r.driver})")
        self._panel(r).connect_dev()

    def disconnect(self, r: InstrumentRow) -> None:
        if r in self._pending:
            self._pending.remove(r)
        if r.panel is not None:
            r.panel.disconnect_dev()

    def connect_all(self) -> None:
        """Connect every row that isn't connected and has a valid config. A
        simulated lock-in waits until the source it measures is up."""
        for r in self.rows:
            if not self._can_connect(r) or r in self._pending:
                continue
            if self._source_of(r):
                self._pending.append(r)
            else:
                self.connect(r)
        self._connect_pending()
        self._update_all_buttons()

    def _source_of(self, r: InstrumentRow) -> str:
        """The instrument this row needs connected first ("" for none)."""
        return r.settings.get("source", "") if r.driver == "sim_lockin" else ""

    def _connect_pending(self) -> None:
        for r in list(self._pending):
            if self._source_of(r) in self.devices:
                self._pending.remove(r)
                self.connect(r)

    def disconnect_all(self) -> None:
        """Close every session; the instruments keep their present state."""
        self._pending.clear()
        for r in self.rows:
            if r.busy:
                self.disconnect(r)
        self._update_all_buttons()

    def safe(self, r: InstrumentRow) -> None:
        """The row's safe state (the panel's Safe state), after asking."""
        if not r.connected or r.panel is None:
            return
        if self._ask_safe([r.name], []):
            r.panel.safe()

    def safe_all(self) -> None:
        """safe_state() on every connected instrument, in the config's shutdown order
        first, after asking. One failure doesn't stop the others."""
        live = [r for r in self.rows if r.connected and r.panel is not None]
        if not live:
            return
        devs = {r.name: r.panel.dev for r in live}
        order = [n for n in self.shutdown_order() if n in devs]
        if not self._ask_safe(list(devs), order):
            return
        for r in live:
            r.panel.stop_move()
        self.log("warn", f"safe state: {', '.join(devs)}…")

        def done(errors):
            for e in errors:
                self.log("error", f"safe state: {e}")
            self.log("info", "safe state: done" + (" with errors" if errors else ""))
            for r in live:
                if r.connected:
                    r.panel.poll()
        self.worker.submit(safe_shutdown(devs, order), done,
                           lambda e: self.log("error", f"safe state: {e}"))

    def _ask_safe(self, names: list[str], order: list[str]) -> bool:
        r = QtWidgets.QMessageBox.question(
            self, "Safe state", f"Put {', '.join(names)} in the safe state "
            "(safe_state())" + (f", in the order {', '.join(order)} first" if order else "")
            + "?")
        return r == QtWidgets.QMessageBox.StandardButton.Yes

    def open_panel(self, r: InstrumentRow) -> PanelWindow:
        if r.window is None:
            r.window = PanelWindow(self._panel(r), r.resource or "simulated")
            n = sum(1 for x in self.rows if x.window is not None)
            r.window.move(self.x() + 60 + 30 * n, self.y() + 30 * n)
        r.window.show()
        r.window.raise_()
        r.window.activateWindow()
        return r.window

    def open_measurement(self) -> QtWidgets.QMainWindow:
        """The General Measurement window (one, kept while the server runs)."""
        from ..measurement import GeneralMeasurementWindow
        if self.measurement is None:
            self.measurement = GeneralMeasurementWindow(self)
        self.measurement.show()
        self.measurement.raise_()
        return self.measurement

    def _drop_panel(self, r: InstrumentRow) -> None:
        """Forget a disconnected row's panel (its name or driver changed)."""
        if r.window is not None:
            r.window.hide()
            r.window.deleteLater()
        elif r.panel is not None:
            r.panel.deleteLater()
        r.panel = r.window = None
        r.state = "disconnected"

    def _drop_panels(self) -> None:
        for r in self.rows:
            self._drop_panel(r)

    # -- closing -------------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.measurement is not None and self.measurement.running:
            QtWidgets.QMessageBox.information(self, "Close", "A measurement is running in "
                                              "General Measurement: stop it first.")
            event.ignore()
            return
        live = [r for r in self.rows if r.connected]
        if live:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Close")
            box.setText(f"{len(live)} instrument(s) connected: "
                        f"{', '.join(r.name for r in live)}.")
            box.setInformativeText("Leave them in their present state, or put them in their "
                                   "safe state first?")
            leave = box.addButton("Leave as is", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            safe = box.addButton("Safe state, then close",
                                 QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() not in (leave, safe):
                event.ignore()
                return
            for r in live:
                p = r.panel
                assert p is not None
                p.timer.stop()
                p.stop_move()
                if box.clickedButton() is safe:
                    try:
                        self.worker.run(p.dev.safe_state(), 120)
                        self.log("info", f"{p.name}: safe state: done")
                    except Exception as e:  # noqa: BLE001
                        QtWidgets.QMessageBox.warning(self, "Safe state",
                                                      f"{p.name}: {type(e).__name__}: {e}")
        if not self._maybe_save():
            event.ignore()
            return
        for r in self.rows:
            if r.panel is not None:
                r.panel.timer.stop()
                if r.connected:
                    r.panel.disconnect_dev()
            if r.window is not None:
                r.window.close()
        if self.measurement is not None:
            self.measurement.close()
        self.worker.stop()
        event.accept()


def open_last(settings) -> bool:
    return settings.value(OPEN_LAST_KEY, True) in (True, "true")


def start_config(settings=None) -> Path | None:
    """The config to open at start: the last one used (misc.toml), unless that
    option is off or the file is gone; then lab.toml, if there is one."""
    settings = settings if settings is not None else appsettings.misc()
    last = settings.value(LAST_KEY, "", str)
    if open_last(settings) and last and Path(last).is_file():
        return Path(last)
    return CONFIG_DIR / "lab.toml" if (CONFIG_DIR / "lab.toml").exists() else None


def main(path: str | Path | None = None) -> int:  # pragma: no cover
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("GeneralMeasurements")
    server = InstrumentServer(path if path is not None else start_config())
    server.show()
    return app.exec()
