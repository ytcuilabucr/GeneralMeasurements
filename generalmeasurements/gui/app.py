"""Instrument control windows for a station: one window per instrument type.

    gm gui --station station.lab.toml

A small station window (toolbar, window list, log) and one window per kind of
instrument: Keithley electronics, temperature controller, magnet supply, RF
source, lock-ins. Instruments of the same kind are docks in the same window
(Qt Advanced Docking System): tabbed at first, and they can be dragged side by
side, split, or floated out. `window = "..."` on a device in the station TOML
puts it in another window. Layouts are saved per station when the GUI closes.

The GUI owns the instrument sessions while it is open (single-owner rule):
don't run a scan on the same instruments at the same time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pyqtgraph  # noqa: F401  (after gui/__init__ pins the binding)
import PySide6QtAds as ads
from PySide6 import QtCore, QtGui, QtWidgets

from ..config import load_station
from ..config.registry import close_devices
from ..config.schema import SimLockinCfg
from ..engine.runner import safe_shutdown
from .hub import DataHub
from .panels import Panel
from .registry import panel_for
from .views import KINDS, FieldPicker, ViewField, ViewWindow
from .worker import AsyncWorker

# Default window for each driver; a device's `window` key overrides it.
WINDOW_OF_DRIVER = {
    "keithley_24xx": "Keithley", "keithley_2000": "Keithley",
    "lakeshore_335": "Temperature",
    "lakeshore_625": "Magnet", "sim_magnet": "Magnet",
    "lm510": "Helium level",
    "gigatronics_2420b": "RF source",
    "sr830": "Lock-in", "sr850": "Lock-in", "sim_lockin": "Lock-in",
    "sim_source": "Sources",
}
LAYOUT_VERSION = 1
_COLORS = {"info": "", "warn": "#b9770e", "error": "#c0392b"}
_DOT = {"connected": "#1e8449", "error": "#c0392b", "connecting…": "#b9770e"}


def window_of(cfg) -> str:
    return str(cfg.window or WINDOW_OF_DRIVER.get(cfg.driver, cfg.driver))


def _configure_ads() -> None:
    """Global QtAds flags; must be set before the first CDockManager exists."""
    flag = ads.CDockManager.eConfigFlag
    ads.CDockManager.setConfigFlags(ads.CDockManager.configFlags()
                                    | flag.OpaqueSplitterResize | flag.FocusHighlighting
                                    | flag.AlwaysShowTabs | flag.DockAreaHasUndockButton)
    ads.CDockManager.setConfigFlag(flag.DockAreaHasCloseButton, False)
    ads.CDockManager.setConfigFlag(flag.XmlCompressionEnabled, False)


class InstrumentWindow(QtWidgets.QMainWindow):
    """One window, one CDockManager; a dock per instrument panel. Closing it only
    hides it (the sessions stay open); the station window shows it again."""

    def __init__(self, title: str, panels: list[Panel], station: StationWindow):
        super().__init__()
        self.title, self.panels, self.station = title, panels, station
        self.setObjectName(f"window_{title}")
        self.setWindowTitle(f"{title} — {station.cfg.station.name}")
        self.resize(780, 860)
        self._hidden_floats: list[QtWidgets.QWidget] = []

        bar = self.addToolBar(title)
        bar.setObjectName("tools")
        for text, slot, tip in (
                ("Connect", self.connect_all, "Open every session in this window and read "
                                              "its state (no resets, no writes)"),
                ("Disconnect", self.disconnect_all, "Close sessions; instruments keep their "
                                                    "present state"),
                ("Safe state", self.all_safe, "safe_state() on every connected instrument in "
                                              "this window"),
                ("Station", station.show_raise, "Bring the station window to the front")):
            act = QtGui.QAction(text, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            bar.addAction(act)

        self.docks = ads.CDockManager(self)               # becomes the central widget
        area = None
        for p in panels:
            dock = ads.CDockWidget(self.docks, p.name)
            dock.setObjectName(p.name)                    # saveState() keys on this
            dock.setWidget(p)                             # wrapped in a scroll area
            dock.setFeature(ads.CDockWidget.DockWidgetFeature.DockWidgetClosable, False)
            dock.setTabToolTip(f"{p.cfg.driver}  {getattr(p.cfg, 'resource', 'simulated')}")
            if area is None:
                area = self.docks.addDockWidget(ads.DockWidgetArea.CenterDockWidgetArea, dock)
            else:
                self.docks.addDockWidgetTabToArea(dock, area)
        if panels:
            self.docks.findDockWidget(panels[0].name).setAsCurrentTab()
        self.default_layout = self.docks.saveState(LAYOUT_VERSION)    # for Reset layout

    def dock(self, name: str) -> ads.CDockWidget:
        return self.docks.findDockWidget(name)

    def connect_all(self) -> None:
        self.station.connect_panels(self.panels)

    def disconnect_all(self) -> None:
        for p in self.panels:
            if p.dev is not None:
                p.disconnect_dev()

    def all_safe(self) -> None:
        self.station.safe_panels(self.panels, self)

    # floating docks are their own top-level windows: hide and show them with this one
    def closeEvent(self, event) -> None:
        self._hidden_floats = [f for f in self.docks.floatingWidgets() if f.isVisible()]
        for f in self._hidden_floats:
            f.hide()
        event.accept()

    def showEvent(self, event) -> None:
        for f in self._hidden_floats:
            f.show()
        self._hidden_floats = []
        super().showEvent(event)

    # -- layout ----------------------------------------------------------------------
    def save_layout(self, s: QtCore.QSettings) -> None:
        s.beginGroup(self.title)
        s.setValue("geometry", self.saveGeometry())
        s.setValue("docks", self.docks.saveState(LAYOUT_VERSION))
        s.setValue("names", ",".join(sorted(p.name for p in self.panels)))
        s.setValue("visible", self.isVisible())
        s.endGroup()

    def restore_layout(self, s: QtCore.QSettings) -> bool:
        """Restore geometry and docks; skipped when the window's instruments changed."""
        s.beginGroup(self.title)
        try:
            if s.value("names") != ",".join(sorted(p.name for p in self.panels)):
                return False
            geo, docks = s.value("geometry"), s.value("docks")
            if geo is not None:
                self.restoreGeometry(geo)
            if docks is not None:
                self.docks.restoreState(docks, LAYOUT_VERSION)
            return True
        finally:
            s.endGroup()


class StationWindow(QtWidgets.QMainWindow):
    """Owns the worker, the sessions, the panels and the data hub; the instrument
    windows show the panels, monitor and chart windows show the hub."""

    def __init__(self, station: str | Path, settings: QtCore.QSettings | None = None,
                 reset_layout: bool = False):
        super().__init__()
        self.cfg = load_station(station)
        self.worker = AsyncWorker()
        self.hub = DataHub()
        self.devices: dict[str, object] = {}
        self.panels: dict[str, Panel] = {}
        self.windows: dict[str, InstrumentWindow] = {}
        self.views: dict[str, ViewWindow] = {}
        self._closing = False
        self.settings = settings or QtCore.QSettings(
            QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope,
            "GeneralMeasurements", "gui")
        if reset_layout:
            self._forget_layout()
        self.setObjectName("station")
        self.setWindowTitle(f"GeneralMeasurements — {self.cfg.station.name} "
                            f"({Path(station).name})")
        self.resize(560, 620)
        self._pending: list[str] = []

        bar = self.addToolBar("Station")
        bar.setObjectName("station")
        for text, slot, tip in (
                ("Connect all", self.connect_all, "Open every session and read its state "
                                                  "(no resets, no writes)"),
                ("Disconnect all", self.disconnect_all, "Close sessions; instruments keep "
                                                        "their present state"),
                ("All safe state", self.all_safe, "safe_state() on every connected device, "
                                                  "in the station's shutdown order"),
                ("Show all windows", self.show_all, "Show every instrument window"),
                ("New monitor…", lambda: self.new_view("monitor"),
                 "A window of big numbers for chosen fields (read only)"),
                ("New chart…", lambda: self.new_view("chart"),
                 "A live chart of chosen fields, several instruments together (read only)"),
                ("Reset layout", self.reset_layout, "Tab every instrument back into its "
                                                    "window and cascade the windows")):
            act = QtGui.QAction(text, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            bar.addAction(act)

        # panels, grouped into windows in station order
        groups: dict[str, list[Panel]] = {}
        for name, d in self.cfg.devices.items():
            panel = panel_for(d.driver)(name, d, self)
            panel.connected.connect(self._on_connected)
            panel.status_changed.connect(self._on_status)
            self.hub.attach(panel)
            self.panels[name] = panel
            groups.setdefault(window_of(d), []).append(panel)
        _configure_ads()
        self.window_of: dict[str, str] = {}
        for title, panels in groups.items():
            self.windows[title] = InstrumentWindow(title, panels, self)
            self.window_of.update({p.name: title for p in panels})

        # overview: a row per window, a status dot per instrument
        overview = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(overview)
        grid.setColumnStretch(1, 1)
        self._dots: dict[str, QtWidgets.QLabel] = {}
        for row, (title, win) in enumerate(self.windows.items()):
            btn = QtWidgets.QPushButton(title)
            btn.setToolTip(f"Show the {title} window")
            btn.clicked.connect(lambda _=False, w=win: self.show_window(w))
            grid.addWidget(btn, row, 0)
            names = QtWidgets.QHBoxLayout()
            for p in win.panels:
                dot = QtWidgets.QLabel()
                self._dots[p.name] = dot
                self._on_status(p.name, p.conn_state)
                names.addWidget(dot)
            names.addStretch()
            grid.addLayout(names, row, 1)
        grid.setRowStretch(len(self.windows), 1)

        self.log_view = QtWidgets.QTextEdit(readOnly=True)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(overview)
        split.addWidget(self.log_view)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self._restore()
        self.log("info", f"station {self.cfg.station.name}: "
                         + "; ".join(f"{t}: {', '.join(p.name for p in w.panels)}"
                                     for t, w in self.windows.items()))

    # -- log -----------------------------------------------------------------------
    def log(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        color = _COLORS.get(level, "")
        line = f"{stamp}  {text}".replace("&", "&amp;").replace("<", "&lt;")
        self.log_view.append(f"<span style='color:{color}'>{line}</span>" if color else line)
        self.statusBar().showMessage(text, 8000)
        win = self.windows.get(self.window_of.get(text.split(":", 1)[0], ""))
        if win is not None:                    # also in the instrument's own window
            win.statusBar().showMessage(text, 8000)

    def _on_status(self, name: str, state: str) -> None:
        color = _DOT.get(state, "#888")
        self._dots[name].setText(f"<span style='color:{color}'>●</span> {name}")
        self._dots[name].setToolTip(state)

    # -- windows -------------------------------------------------------------------
    def show_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def show_window(self, win: InstrumentWindow) -> None:
        win.show()
        win.raise_()
        win.activateWindow()

    def show_all(self) -> None:
        for win in self.windows.values():
            self.show_window(win)

    def _cascade(self, wins=None) -> None:
        x, y = self.x() + self.width() + 20, self.y()
        for win in wins if wins is not None else [*self.windows.values(),
                                                  *self.views.values()]:
            win.move(x, y)
            x, y = x + 40, y + 40

    def _forget_layout(self) -> None:
        self.settings.beginGroup(self.cfg.station.name)
        self.settings.remove("")                 # this station's saved layout only
        self.settings.endGroup()

    def reset_layout(self) -> None:
        for win in self.windows.values():
            win.docks.restoreState(win.default_layout, LAYOUT_VERSION)
            win.resize(780, 860)
        for v in self.views.values():          # open views stay open, re-docked
            v.docks.restoreState(v.default_layout)
        self._forget_layout()
        self._cascade()
        self.log("info", "layout reset")

    # -- monitor and chart windows -----------------------------------------------------------
    def new_view(self, kind: str) -> ViewWindow | None:
        fields = self.hub.fields()
        if not fields:
            QtWidgets.QMessageBox.information(
                self, KINDS[kind], "Connect an instrument first: the list of fields comes "
                "from the connected instruments.")
            return None
        dlg = FieldPicker(kind, fields, parent=self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return self.open_view(kind, dlg.result_fields())

    def open_view(self, kind: str, fields: list[ViewField], title: str | None = None,
                  show: bool = True) -> ViewWindow:
        if title is None:
            n = 1
            while f"{KINDS[kind]} {n}" in self.views:
                n += 1
            title = f"{KINDS[kind]} {n}"
        win = ViewWindow(kind, title, fields, self)
        self.views[title] = win
        if show:
            self._cascade([win])
            self.show_window(win)
            self.log("info", f"{title}: {', '.join(f.key for f in fields)}")
        return win

    def remove_view(self, win: ViewWindow) -> None:
        if self._closing:                        # closing the GUI: layout already saved
            return
        if self.views.get(win.title) is win:
            del self.views[win.title]
            win.view.detach()
            win.deleteLater()

    def _restore(self) -> None:
        s = self.settings
        s.beginGroup(self.cfg.station.name)
        try:
            geo = s.value("station/geometry")
            if geo is not None:
                self.restoreGeometry(geo)
            fresh = [w for w in self.windows.values() if not w.restore_layout(s)]
            self._show_later = [w for w in self.windows.values()
                                if w not in fresh and s.value(f"{w.title}/visible") in
                                (True, "true")]
            if fresh:
                self._cascade(fresh)
                self._show_later += fresh
            try:
                saved = json.loads(s.value("views/list") or "[]")
            except ValueError:
                saved = []
            for v in saved:
                try:
                    fields = [ViewField(**f) for f in v["fields"]]
                    win = self.open_view(v["kind"], fields, v["title"], show=False)
                except (KeyError, TypeError) as e:     # from an older or edited file
                    self.log("warn", f"saved view skipped: {e}")
                    continue
                win.restore_layout(s)
                self._show_later.append(win)
        finally:
            s.endGroup()

    def save_layout(self) -> None:
        s = self.settings
        s.beginGroup(self.cfg.station.name)
        s.setValue("station/geometry", self.saveGeometry())
        for w in self.windows.values():
            w.save_layout(s)
        s.remove("views")                        # views closed since are forgotten
        s.setValue("views/list", json.dumps([v.config() for v in self.views.values()]))
        for v in self.views.values():
            v.save_layout(s)
        s.endGroup()
        s.sync()

    def show(self) -> None:
        super().show()
        for w in getattr(self, "_show_later", []):
            w.show()
        self._show_later = []

    # -- station actions -------------------------------------------------------------
    def connect_panels(self, panels: list[Panel]) -> None:
        # sims that need another device connect once it is up
        for p in panels:
            if isinstance(p.cfg, SimLockinCfg):
                if p.name not in self._pending:
                    self._pending.append(p.name)
            else:
                p.connect_dev()
        self._on_connected("")

    def connect_all(self) -> None:
        self.connect_panels(list(self.panels.values()))

    def _on_connected(self, _name: str) -> None:
        for name in list(self._pending):
            if self.cfg.devices[name].source in self.devices:
                self._pending.remove(name)
                self.panels[name].connect_dev()

    def disconnect_all(self) -> None:
        self._pending.clear()
        for panel in self.panels.values():
            if panel.dev is not None:
                panel.disconnect_dev()

    def all_safe(self) -> None:
        self.safe_panels(list(self.panels.values()), self)

    def safe_panels(self, panels: list[Panel], parent: QtWidgets.QWidget) -> None:
        devs = {p.name: self.devices[p.name] for p in panels if p.name in self.devices}
        if not devs:
            return
        order = [n for n in self.cfg.station.shutdown_order if n in devs]
        r = QtWidgets.QMessageBox.question(
            parent, "Safe state", f"Call safe_state() on {', '.join(devs)}"
            + (f", in the order {order} first" if order else "") + "?")
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        for p in panels:
            p.stop_move()
        self.log("warn", f"safe state: {', '.join(devs)}…")

        def done(errors):
            for e in errors:
                self.log("error", f"safe state: {e}")
            self.log("info", "safe state: done" + (" with errors" if errors else ""))
        self.worker.submit(safe_shutdown(devs, order), done,
                           lambda e: self.log("error", f"safe state: {e}"))

    # -- close -----------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.devices:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Close")
            box.setText("Instruments are connected.")
            box.setInformativeText("Leave them in their present state, or put every one in "
                                   "its safe state first?")
            leave = box.addButton("Leave as is", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            safe = box.addButton("Safe state, then close",
                                 QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked not in (leave, safe):
                event.ignore()
                return
            for p in self.panels.values():
                p.timer.stop()
                p.stop_move()
            try:
                if clicked is safe:
                    errs = self.worker.run(safe_shutdown(dict(self.devices),
                                                         self.cfg.station.shutdown_order), 120)
                    if errs:
                        QtWidgets.QMessageBox.warning(self, "Safe state", "\n".join(errs))
                self.worker.run(close_devices(dict(self.devices)), 10)
            except Exception as e:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(self, "Close", f"{type(e).__name__}: {e}")
        self.save_layout()
        self._closing = True
        for w in [*self.windows.values(), *self.views.values()]:
            w.close()                           # hides its floating docks too
        self.worker.stop()
        event.accept()


def main(station: str | Path, reset_layout: bool = False) -> int:  # pragma: no cover
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setApplicationName("GeneralMeasurements")
    win = StationWindow(station, reset_layout=reset_layout)
    win.show()
    return app.exec()
