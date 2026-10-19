"""One panel per instrument: live readout, trend, and its essential controls.

Connecting never resets an instrument: a panel opens the session and reads the
present state (attach). Anything that writes goes through the driver, so the
station's limits and ramp rates apply exactly as in a scan; actions that
energise something (output on, RF on, magnet ramp, heater on) ask first.

Most instruments use `DefinitionPanel`, built from the driver's definition
(devices/defs/*.toml); the cryostat instruments have their own look
(gui/common/dedicated_panels/). Shared by the Instrument Server and, later, General
Measurement.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, ClassVar

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ...config.registry import build_device
from ...devices.definitions import (LEVELS, Definition, Prop, bounds, choices, definition_of,
                                    field_key, get_value, label_of, number, props,
                                    read_connect, run, text, write)
from ...units import SI_UNITS, fmt, parse_si
from .uiload import load_ui
from .widgets import Fold, Led

__all__ = ["DefinitionPanel", "GenericPanel", "Panel", "SIEdit", "detail_actions", "fmt",
           "parse_si"]
_NOW = object()            # display(): "the present value"


class SIEdit(QtWidgets.QLineEdit):
    """A number with SI prefixes ("100n"). In Designer: promoted QLineEdit,
    property `unit` (sets the hint)."""

    def __init__(self, value: float | None = None, unit: str = "", width: int = 110):
        super().__init__("" if value is None else fmt(value, unit).replace(" ", ""))
        self.setFixedWidth(width)
        self._set_unit(unit)

    def _set_unit(self, unit: str) -> None:
        self._unit = unit
        self.setPlaceholderText(f"e.g. 100m{unit}" if unit in SI_UNITS else "value")

    unit = QtCore.Property(str, lambda self: self._unit, _set_unit)

    def value(self) -> float:
        return parse_si(self.text())


class TrendChart(QtCore.QObject):
    """One folded chart of a panel: a ▸ heading (closed by default), a channel
    drop-down, Auto zoom, and the plot of that channel against time."""

    changed = QtCore.Signal()                    # another channel chosen, or opened

    def __init__(self, n: int, channels: list[tuple[str, str, str]], key: str):
        super().__init__()
        self.fold = Fold(f"Chart {n}", "A live chart of one reading; closed charts cost "
                                       "nothing")
        self.combo = QtWidgets.QComboBox()
        self.combo.setToolTip("The reading to chart")
        for k, label, unit in channels:
            self.combo.addItem(f"{label} ({unit})" if unit else label, (k, label, unit))
        self.zoom = QtWidgets.QPushButton("Auto zoom")
        self.zoom.setToolTip("Back to the default view: fit all the data, and follow it")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.combo, stretch=1)
        row.addWidget(self.zoom)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(100)      # the splitter sets the height
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "time", "s")
        self.curve = self.plot.plot(pen=pg.mkPen(width=2))
        self.fold.form.addRow(row)
        self.fold.form.addRow(self.plot)
        self.combo.setCurrentIndex(max(0, [c[0] for c in channels].index(key)))
        self._label()
        self.combo.currentIndexChanged.connect(self._new_channel)
        self.zoom.clicked.connect(self.auto_zoom)
        self.fold.toggled.connect(lambda on: on and self.changed.emit())

    def channel(self) -> str:
        return self.combo.currentData()[0]

    def _label(self) -> None:
        _k, label, unit = self.combo.currentData()
        self.plot.setLabel("left", label, unit)

    def _new_channel(self) -> None:
        self._label()
        self.curve.setData([], [])
        self.auto_zoom()
        self.changed.emit()

    def auto_zoom(self) -> None:
        """The default view: autorange both axes (and keep following the data)."""
        self.plot.getPlotItem().enableAutoRange()

    def show_points(self, points) -> None:
        if points:
            xs, ys = zip(*points)
            self.curve.setData(xs, ys)
        else:
            self.curve.setData([], [])


# -- base ------------------------------------------------------------------------
class Panel(QtWidgets.QWidget):
    """Shared plumbing. Subclasses override build_controls(), attach(),
    after_attach() and, if needed, read_coro(). A panel with its own look has its
    own form (UI = "<name>" for a ui/<name>.ui form, with the same header and footer
    widget names) and overrides setup_ui() and show_data() instead of using the
    default readings grid and trend."""

    connected = QtCore.Signal(str)
    status_changed = QtCore.Signal(str, str)    # name, state
    # name, wall-clock time, the poll's data. `object`, not `dict`: a dict would
    # be converted to a QVariantMap, mangling non-float extras such as tuples.
    reading = QtCore.Signal(str, float, object)
    STATE_COLORS: ClassVar[dict[str, str]] = {"connected": "#1e8449", "error": "#c0392b"}
    DEFAULT_INTERVAL = 1.0                      # s between polls
    MAX_INTERVAL = 60.0
    FOLDS = False                               # has the Essential / Expanded / All folds
    UI = "panel"                                # the form: forms/<UI>.ui
    ctrl_box: QtWidgets.QGroupBox | None = None    # in the form, if it has a Control box
    form: QtWidgets.QFormLayout | None = None
    charts_split: QtWidgets.QSplitter | None = None   # in the form, if it has charts
    btn_update: QtWidgets.QPushButton | None = None   # in the form: Update settings

    def __init__(self, name: str, cfg, station):
        super().__init__()
        self.name, self.cfg, self.station = name, cfg, station
        self.dev: Any = None
        self.detail = 0                         # 0 essential, 1 expanded, 2 all
        self._reading = False
        self._last_error = ""
        self._move = None
        self._t0 = 0.0
        self._labels: dict[str, tuple[QtWidgets.QLabel, str]] = {}
        self._names: dict[str, str] = {}        # field key -> label, for the chart choices
        self._history: dict[str, deque[tuple[float, float]]] = {}
        self.charts: list[TrendChart] = []
        self.conn_state = "disconnected"

        load_ui(self.UI, self)                  # title, status, btn_connect, ... btn_safe
        res = getattr(cfg, "resource", "simulated")
        self.title.setText(f"<b>{name}</b> &nbsp; {cfg.driver} &nbsp; {res}")
        self.btn_connect.clicked.connect(self.connect_dev)
        self.btn_disconnect.clicked.connect(self.disconnect_dev)
        self.live.toggled.connect(self._live_toggled)
        self.interval.setRange(0.2, self.MAX_INTERVAL)
        self.interval.setValue(self.DEFAULT_INTERVAL)
        self.interval.valueChanged.connect(lambda v: self.timer.setInterval(int(v * 1000)))
        self.btn_read.clicked.connect(self.poll)
        self.btn_safe.setToolTip(self.safe_tip())
        self.btn_safe.clicked.connect(self.safe)
        if self.btn_update is not None:
            self.btn_update.clicked.connect(self.update_settings)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(int(self.DEFAULT_INTERVAL * 1000))
        self.timer.timeout.connect(self.poll)
        self.setup_ui()
        if self.form is not None:
            self.build_controls(self.form)
        self._set_enabled(False)

    # -- for subclasses ------------------------------------------------------------
    def setup_ui(self) -> None:
        """Configure the loaded form. Default look (panel.ui): the charts are
        built once the readings are known (build_charts)."""

    def build_controls(self, form: QtWidgets.QFormLayout) -> None:
        pass

    async def attach(self) -> Any:
        """Read-only state sync after the session opens; result goes to after_attach()."""
        return None

    def after_attach(self, result: Any) -> None:
        pass

    async def read_coro(self) -> dict | None:
        return await self.dev.read()

    def safe_tip(self) -> str:
        return "Call the driver's safe_state()"

    def set_detail(self, level: int) -> None:
        """How much to show: 0 essential, 1 expanded, 2 all (panels with FOLDS)."""
        self.detail = level

    # -- helpers -------------------------------------------------------------------
    def call(self, coro, what: str, on_done=None, quiet: bool = False):
        if not quiet:
            self.station.log("info", f"{self.name}: {what}")
        return self.station.worker.submit(coro, on_done, lambda e: self._error(what, e))

    def move(self, coro, what: str, on_done=None) -> None:
        """A long action (ramp) that a Stop button can cancel."""
        def done(r):
            self._move = None
            self.station.log("info", f"{self.name}: {what}: done")
            if on_done:
                on_done(r)
        self._move = self.call(coro, what, done)

    def stop_move(self) -> None:
        if self._move is not None:
            self._move.cancel()
            self.station.log("warn", f"{self.name}: stopped by operator")
            self._move = None

    def confirm(self, text: str) -> bool:
        r = QtWidgets.QMessageBox.question(self, f"{self.name}: confirm", text)
        return r == QtWidgets.QMessageBox.StandardButton.Yes

    def value_of(self, edit: SIEdit, what: str) -> float | None:
        try:
            return edit.value()
        except ValueError as e:
            self.station.log("error", f"{self.name}: {what}: {e}")
            return None

    def _error(self, what: str, e: BaseException) -> None:
        if what == "read":
            self._reading = False
        if type(e).__name__ == "CancelledError":
            return
        msg = f"{type(e).__name__}: {e}"
        self.set_status("error", msg)
        if what != "read" or msg != self._last_error:     # don't flood the log while polling
            self.station.log("error", f"{self.name}: {what}: {msg}")
        self._last_error = msg
        if what == "connect":
            self.btn_connect.setEnabled(True)
        elif what == "read present state":       # opened but unusable: close it again
            self.disconnect_dev()
            self.set_status("error", msg)

    def set_status(self, state: str, tip: str = "") -> None:
        color = self.STATE_COLORS.get(state)
        self.status.setText(f"<span style='color:{color}'>{state}</span>" if color else state)
        self.status.setToolTip(tip)
        self.conn_state = state
        self.status_changed.emit(self.name, state)

    def _set_enabled(self, on: bool) -> None:
        if self.ctrl_box is not None:
            self.ctrl_box.setEnabled(on)
        for w in (self.btn_read, self.btn_safe, self.btn_disconnect):
            w.setEnabled(on)
        self.btn_connect.setEnabled(not on)

    def build_readouts(self) -> None:
        """Default look: (re)build the readings grid from describe()."""
        while self.readings.count():
            item = self.readings.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._labels.clear()
        self._names.clear()
        for row, (key, info) in enumerate(self.dev.describe().items()):
            name = QtWidgets.QLabel(info.label or key)
            val = QtWidgets.QLabel("—")
            val.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setStyleSheet("font-size: 15pt; font-family: Consolas, monospace;")
            self.readings.addWidget(name, row, 0)
            self.readings.addWidget(val, row, 1)
            self._labels[key] = (val, info.units)
            self._names[key] = info.label or key

    # -- charts --------------------------------------------------------------------
    MAX_CHARTS = 4

    def build_charts(self) -> None:
        """Up to MAX_CHARTS folded charts, one per reading channel (fewer when the
        instrument has fewer), each with its own channel choice. Rebuilt when the
        readings change (connect, mode switch); a chart keeps its channel and
        whether it is open when that channel still exists."""
        if self.charts_split is None:
            return
        keep = [(c.channel(), c.fold.is_open()) for c in self.charts]
        for c in self.charts:
            c.fold.setParent(None)
            c.fold.deleteLater()
        channels = [(k, self._names.get(k, k), self._labels[k][1]) for k in self._labels]
        self._history = {k: self._history.get(k, deque(maxlen=3600)) for k, _, _ in channels}
        keys = [k for k, _, _ in channels]
        self.charts = []
        for i in range(min(self.MAX_CHARTS, len(channels))):
            key, was_open = keep[i] if i < len(keep) and keep[i][0] in keys else (keys[i], False)
            c = TrendChart(i + 1, channels, key)
            c.fold.set_open(was_open)
            c.changed.connect(lambda c=c: self._draw(c))
            self.charts_split.addWidget(c.fold)
            self.charts.append(c)

    def _draw(self, c: TrendChart) -> None:
        if c.fold.is_open():
            c.show_points(self._history.get(c.channel(), ()))

    def _record(self, data: dict) -> None:
        """Add a poll to every channel's history, then redraw the open charts."""
        t = time.monotonic() - self._t0
        for key, hist in self._history.items():
            v = data.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and abs(f) < 9e37:          # 9.9e37: overrange
                hist.append((t, f))
        for c in self.charts:
            self._draw(c)

    # -- lifecycle -----------------------------------------------------------------
    def connect_dev(self) -> None:
        if self.dev is not None:
            return
        self.btn_connect.setEnabled(False)
        self.set_status("connecting…")
        self.call(build_device(self.name, self.cfg, self.station.devices), "connect", self._opened)

    def _opened(self, dev) -> None:
        self.dev = dev
        self.station.devices[self.name] = dev
        self.build_readouts()
        self.call(self.attach(), "read present state", self._attached)

    def _attached(self, result) -> None:
        self.set_status("connected")
        self._set_enabled(True)
        self._t0 = time.monotonic()
        self._history.clear()
        self.after_attach(result)
        self.build_charts()
        self.connected.emit(self.name)
        self.poll()
        if self.live.isChecked():
            self.timer.start()

    def disconnect_dev(self) -> None:
        self.timer.stop()
        self.stop_move()
        dev, self.dev = self.dev, None
        self.station.devices.pop(self.name, None)
        self._set_enabled(False)
        self.set_status("disconnected")
        t = getattr(dev, "t", None)
        if t is not None:
            self.call(t.close(), "disconnect")

    def _live_toggled(self, on: bool) -> None:
        if on and self.dev is not None:
            self.timer.start()
        else:
            self.timer.stop()

    def poll(self) -> None:
        if self.dev is None or self._reading:
            return
        self._reading = True
        self.call(self.read_coro(), "read", self._got, quiet=True)

    def _got(self, data: dict | None) -> None:
        self._reading = False
        if self._last_error:
            self._last_error = ""
            self.set_status("connected")
        if data:                    # the one place readings leave the panel (DataHub)
            self.reading.emit(self.name, time.time(), data)
        self.show_data(data)
        self.update_state()

    def show_data(self, data: dict | None) -> None:
        """Default look: one label per reading, and the charts."""
        for key, (label, unit) in self._labels.items():
            label.setText(fmt(data.get(key), unit) if data else "—")
        if data:
            self._record(data)

    def update_state(self) -> None:
        """Refresh state labels from driver beliefs (subclasses)."""

    def update_settings(self) -> None:
        """Read the settings from the instrument again (Update settings): the
        connect-time state read, queries only. The standard panel re-reads its
        settings rows instead."""
        if self.dev is not None:
            self.call(self.attach(), "update settings", self.after_attach)

    def safe(self) -> None:
        self.stop_move()
        self.call(self.dev.safe_state(), "safe state", lambda _: self.poll())


# -- generic (simulators, any Movable/Rampable) ------------------------------------
class GenericPanel(Panel):
    def build_controls(self, form):
        self.target = SIEdit()
        self.rate = SIEdit()
        go = QtWidgets.QPushButton("Go")
        go.clicked.connect(self._go)
        stop = QtWidgets.QPushButton("Stop")
        stop.clicked.connect(self.stop_move)
        row = QtWidgets.QHBoxLayout()
        for w in (self.target, QtWidgets.QLabel("rate /s"), self.rate, go, stop):
            row.addWidget(w)
        row.addStretch()
        self._row = row
        form.addRow("Target", row)

    def after_attach(self, _):
        lim = getattr(self.dev, "limits", None)
        movable = lim is not None and hasattr(self.dev, "set")
        self.ctrl_box.setVisible(movable)
        if movable:
            self.info.setText(f"limits {lim}")
            self.rate.setEnabled(hasattr(self.dev, "start_ramp"))
            if hasattr(self.dev, "max_rate"):
                self.rate.setText(f"{self.dev.max_rate:g}")

    def _go(self):
        v = self.value_of(self.target, "target")
        if v is None:
            return
        if hasattr(self.dev, "start_ramp") and self.rate.text():
            r = self.value_of(self.rate, "rate")
            if r is not None:
                self.call(self.dev.start_ramp(v, r), f"ramp to {v:g} at {r:g}/s")
        else:
            self.move(self.dev.set(v), f"set {v:g}")

# -- standard panel, built from the driver's definition ------------------------------
class _Row:
    """The widgets of one setting: present value, editor, buttons."""

    def __init__(self, pid: str, p: Prop, unit: str):
        self.pid, self.p, self.unit = pid, p, unit
        self.now = QtWidgets.QLabel("—")
        self.now.setMinimumWidth(110)
        self.now.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.edit: QtWidgets.QWidget | None = None
        self.rate: SIEdit | None = None
        self.controls: list[QtWidgets.QWidget] = []      # disabled when writing isn't allowed


class DefinitionPanel(Panel):
    """The standard look, built from the driver's definition (devices/defs/):
    readings and status lights from the live properties, one row per setting
    (present value, editor, Set / Ramp to, Stop), the actions, and the mode with
    Switch mode… when the driver has one. Confirmations, interlocks and the
    read-only condition come from the definition; limits are enforced by the
    driver, as in a scan.

    Level-0 properties are always shown; levels 1 and 2 are in two nested toggle
    lists (`Fold`), "More" and inside it "All"."""

    FOLDS = True
    FOLD_TITLES: ClassVar[dict[int, str]] = {1: "More", 2: "All"}
    FOLD_TIPS: ClassVar[dict[int, str]] = {1: "Less-used readings and settings",
                                           2: "Everything else: rarely used or read only"}

    def build_controls(self, form):
        self.folds: list[Fold] = []
        self.defn: Definition | None = None
        self.values: dict[str, Any] = {}
        self.rows: dict[str, _Row] = {}
        self._leds: dict[str, tuple[Led, Prop]] = {}
        self._mismatch: str | None = None

    # -- connect -------------------------------------------------------------------
    async def attach(self):
        d = self.dev
        defn = definition_of(d)
        if defn.attach:
            await run(d, defn.attach)
        defn = definition_of(d)              # attach may adopt the instrument's mode
        return (defn, *await self._state(defn))

    async def _state(self, defn: Definition):
        """(the instrument's present mode or None, every `get` property): queries only."""
        present = await run(self.dev, defn.check_mode) if defn.check_mode else None
        return present, await read_connect(defn, self.dev)

    def after_attach(self, result):
        defn, present, values = result
        self.defn = defn
        self.btn_safe.setToolTip(text(defn.safe, self.dev) or self.safe_tip())
        self.build_readouts()
        self._build_settings()
        self._got_state((present, values), announce=True)

    def build_readouts(self):
        """Level-0 readings from the live properties (the folds hold the others);
        a status is a light, not a number."""
        if self.defn is None:
            super().build_readouts()
            return
        while self.readings.count():
            item = self.readings.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._labels.clear()
        self._names.clear()
        self._leds.clear()
        d, row = self.dev, 0
        for pid, p in props(self.defn, d).items():
            if p.source != "live" or p.level != 0:
                continue
            label, val = self._reading_widget(pid, p, "15pt")
            if val is None:
                self.readings.addWidget(label, row, 0, 1, 2)
            else:
                self.readings.addWidget(label, row, 0)
                self.readings.addWidget(val, row, 1)
            row += 1

    def _reading_widget(self, pid: str, p: Prop, size: str):
        """(label, value) for a live property, registered for show_data(); a status
        is one Led (value None)."""
        d = self.dev
        key, label = field_key(d.name, pid, p), text(p.label, d) or pid
        if p.kind == "status":
            led = Led(label)
            self._leds[key] = (led, p)
            return led, None
        val = QtWidgets.QLabel("—")
        val.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setStyleSheet(f"font-size: {size}; font-family: Consolas, monospace;")
        self._labels[key] = (val, text(p.unit, d))
        self._names[key] = label
        return QtWidgets.QLabel(label), val

    def _build_settings(self):
        form, d, defn = self.form, self.dev, self.defn
        assert defn is not None
        while form.rowCount():
            form.removeRow(0)
        self.rows.clear()
        self.info.setText(" — ".join(t for t in (f"<b>{text(defn.label, d)}</b>",
                                                   text(defn.info, d)) if t != "<b></b>" and t))
        if defn.modes:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(f"<b>{defn.modes.get(defn.mode or '', defn.mode)}</b>"))
            if defn.switch and len(defn.modes) > 1:
                btn = QtWidgets.QPushButton("Switch mode…")
                btn.clicked.connect(self.switch_mode)
                row.addWidget(btn)
            row.addStretch()
            form.addRow("Mode", row)
        self._fill_level(form, 0)
        self.folds = []
        here = props(defn, d)
        for level in (1, 2):                    # level 2 folds inside level 1
            if not any(p.level >= level for p in here.values()):
                break
            fold = Fold(self.FOLD_TITLES[level], self.FOLD_TIPS[level])
            fold.set_open(self.detail >= level)
            fold.toggled.connect(self._fold_toggled)
            form.addRow(fold)
            self.folds.append(fold)
            form = fold.form
            self._fill_level(form, level)

    def _fill_level(self, form: QtWidgets.QFormLayout, level: int):
        """One level's rows: its readings (above level 0), settings by group, actions."""
        d, defn = self.dev, self.defn
        assert defn is not None
        here = {pid: p for pid, p in props(defn, d).items() if p.level == level}
        if level:
            for pid, p in here.items():
                if p.source == "live":
                    label, val = self._reading_widget(pid, p, "11pt")
                    if val is None:
                        form.addRow(label)
                    else:
                        form.addRow(label, val)
        shown = {pid: p for pid, p in here.items()
                 if p.kind != "action" and (p.writable or p.source == "connect")}
        groups = [g for g in defn.groups if any(p.group == g for p in shown.values())]
        groups += [g for g in dict.fromkeys(p.group for p in shown.values()) if g not in groups]
        for g in groups:
            if g:
                form.addRow(QtWidgets.QLabel(f"<b>{g}</b>"))
            for pid, p in shown.items():
                if p.group == g:
                    form.addRow(text(p.label, d) or pid, self._make_row(pid, p))
        actions = [(pid, p) for pid, p in here.items() if p.kind == "action"]
        if actions:
            row = QtWidgets.QHBoxLayout()
            for pid, p in actions:
                btn = QtWidgets.QPushButton(text(p.label, d) or pid)
                btn.setToolTip(text(p.tooltip, d))
                btn.clicked.connect(lambda _=False, pid=pid: self.write_prop(pid))
                row.addWidget(btn)
            row.addStretch()
            form.addRow("Actions", row)

    # -- detail level ----------------------------------------------------------------
    def set_detail(self, level: int) -> None:
        self.detail = level
        for i, fold in enumerate(self.folds, start=1):
            fold.set_open(level >= i)

    def _fold_toggled(self, _on: bool) -> None:
        """A fold opened or closed by hand: the level is the deepest open fold
        whose parents are open too."""
        level = 0
        for fold in self.folds:
            if not fold.is_open():
                break
            level += 1
        self.detail = level

    def _make_row(self, pid: str, p: Prop) -> QtWidgets.QWidget:
        d = self.dev
        r = _Row(pid, p, text(p.unit, d))
        self.rows[pid] = r
        box = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(r.now)
        if p.writable and p.kind == "bool":
            for on in (True, False):
                asks = bool(p.confirm) and (not p.confirm_when or on in p.confirm_when)
                btn = QtWidgets.QPushButton(("On" if on else "Off") + ("…" if asks else ""))
                btn.clicked.connect(lambda _=False, on=on: self.write_prop(pid, on))
                r.controls.append(btn)
        elif p.writable and p.kind == "enum":
            combo = QtWidgets.QComboBox()
            for label, v in choices(p, d):
                combo.addItem(label or fmt(v, r.unit), v)
            btn = QtWidgets.QPushButton("Set")
            btn.clicked.connect(lambda: self.write_prop(pid, combo.currentData()))
            r.edit = combo
            r.controls += [combo, btn]
        elif p.writable:
            edit = SIEdit(unit=r.unit) if p.kind in ("float", "int") else QtWidgets.QLineEdit()
            edit.returnPressed.connect(lambda: self._write_from_edit(pid))
            r.edit = edit
            r.controls.append(edit)
            if p.ramp is not None and p.ramp.start:
                r.rate = SIEdit(number(p.ramp.max_rate, d), width=80)
                r.rate.setToolTip(f"rate, at most {number(p.ramp.max_rate, d):g} "
                                  f"{text(p.ramp.unit, d)}")
                r.controls += [QtWidgets.QLabel("at"), r.rate,
                               QtWidgets.QLabel(text(p.ramp.unit, d))]
            btn = QtWidgets.QPushButton("Ramp to" if p.ramp else "Set")
            btn.clicked.connect(lambda: self._write_from_edit(pid))
            r.controls.append(btn)
        for w in r.controls:
            lay.addWidget(w)
        if p.writable and (p.ramp or p.stoppable):     # Stop is never disabled
            stop = QtWidgets.QPushButton("Stop")
            stop.clicked.connect(lambda: self.stop_prop(pid))
            lay.addWidget(stop)
        lay.addStretch()
        tip = [text(p.tooltip, d)]
        lo, hi = bounds(p, d)
        if lo is not None or hi is not None:
            tip.append(f"limits {fmt(lo, r.unit)} … {fmt(hi, r.unit)}")
        if p.readback:
            tip.append(f"read back as {p.readback}")
        box.setToolTip("\n".join(t for t in tip if t))
        return box

    # -- values --------------------------------------------------------------------
    def display(self, pid: str, value: Any = _NOW) -> str:
        """A property's value as text (its present value when `value` is None)."""
        assert self.defn is not None
        p = self.defn.properties[pid]
        v = self.values.get(pid) if value is _NOW else value
        unit = text(p.unit, self.dev)
        if v is None:
            return "auto" if p.allow_auto and (value is not _NOW or pid in self.values) else "—"
        if p.kind in ("bool", "status"):
            return "ON" if v else "off"
        if p.kind == "enum" and p.choices:
            return str(label_of(p, v))
        if p.kind in ("float", "int", "enum"):
            try:
                return fmt(v, unit)
            except (TypeError, ValueError):
                return str(v)
        return str(v)

    def _template(self, template: str, **extra) -> str:
        assert self.defn is not None
        shown = {pid: self.display(pid) for pid in self.defn.properties}
        return text(template, self.dev, **{**shown, **extra})

    def _got_state(self, state, announce: bool = False):
        present, values = state
        self.values.update(values)
        defn = self.defn
        assert defn is not None
        was = self._mismatch
        self._mismatch = present if present is not None and present != defn.mode else None
        if self._mismatch and (announce or not was):
            self.station.log("warn", f"{self.name}: the instrument is in "
                             f"{defn.modes.get(self._mismatch, self._mismatch)}, the station "
                             f"says {defn.modes.get(defn.mode or '', defn.mode)}. Settings are "
                             "disabled until Configure (reset) or Switch mode…")
        self._show_values()

    def show_data(self, data):
        super().show_data(data)
        for key, (led, p) in self._leds.items():
            v = data.get(key) if data else None
            led.set("off" if not v else ("warn" if p.severity == "amber" else "bad"))
        if data and self.defn is not None:
            for pid, p in props(self.defn, self.dev).items():
                key = field_key(self.name, pid, p)
                if p.source == "live" and key in data:
                    self.values[pid] = data[key]
            self._show_values()

    def _show_values(self):
        if self.defn is None:
            return
        for pid, r in self.rows.items():
            now = self.display(pid)
            if r.p.kind == "bool" and self.values.get(pid) and r.p.confirm:
                now = f"<b style='color:#c0392b'>{now}</b>"          # energised
            if r.p.readback and r.p.readback in self.values:
                now += f" · read {self.display(r.p.readback)}"
            r.now.setText(now)
            if r.edit is not None and not r.edit.property("filled") and pid in self.values:
                self._fill(r)
        self._update_enabled()

    def _fill(self, r: _Row):
        """Put the present value into the editor once, as a starting point."""
        v = self.values.get(r.pid)
        if isinstance(r.edit, QtWidgets.QComboBox):
            i = r.edit.findData(v)
            if i >= 0:
                r.edit.setCurrentIndex(i)
        elif isinstance(r.edit, QtWidgets.QLineEdit):
            r.edit.setText("auto" if v is None else
                           str(v) if r.p.kind == "str" else self.display(r.pid).replace(" ", ""))
        if r.edit is not None:
            r.edit.setProperty("filled", True)

    def _update_enabled(self):
        for r in self.rows.values():
            ok = self._mismatch is None
            if r.p.interlock:
                want = not r.p.interlock.startswith("!")
                ok = ok and bool(self.values.get(r.p.interlock.lstrip("!"))) == want
            for w in r.controls:
                w.setEnabled(ok)

    async def read_coro(self):
        defn = self.defn
        if defn is not None and defn.read_if:
            if not await get_value(self.dev, defn.properties[defn.read_if]):
                return None
        return await self.dev.read()

    def update_settings(self):
        self.refresh()

    def refresh(self):
        """Re-read the `get` properties (and the instrument's mode), then poll."""
        if self.dev is None or self.defn is None:
            return
        self.call(self._state(self.defn), "read settings", self._got_state, quiet=True)
        self.poll()

    # -- writing -------------------------------------------------------------------
    def _write_from_edit(self, pid: str):
        r = self.rows[pid]
        assert isinstance(r.edit, QtWidgets.QLineEdit)
        txt = r.edit.text().strip()
        if r.p.kind == "str":
            value: Any = r.edit.text()
        elif r.p.allow_auto and txt.lower() in ("", "auto"):
            value = None
        else:
            value = self.value_of(r.edit, r.pid) if isinstance(r.edit, SIEdit) else None
            if value is None:
                return
            if r.p.kind == "int":
                value = round(value)
            lo, hi = bounds(r.p, self.dev)            # min / max / limits of the definition
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                allowed = (f"{fmt(lo, r.unit)} … {fmt(hi, r.unit)}" if lo is not None
                           and hi is not None else f"at least {fmt(lo, r.unit)}"
                           if lo is not None else f"at most {fmt(hi, r.unit)}")
                self.station.log("error", f"{self.name}: {text(r.p.label, self.dev) or pid} "
                                 f"{fmt(value, r.unit)} refused: allowed {allowed}; not sent")
                return
        rate = None
        if r.rate is not None:
            rate = self.value_of(r.rate, f"{pid} rate")
            if rate is None:
                return
        self.write_prop(pid, value, rate)

    def write_prop(self, pid: str, value: Any = None, rate: float | None = None) -> None:
        """Set a property (or run an action) through the driver, asking first if
        the definition says so."""
        defn, d = self.defn, self.dev
        if defn is None or d is None:
            return
        p = defn.properties[pid]
        shown = self.display(pid, value) if p.kind != "action" else ""
        if p.confirm and (not p.confirm_when or value in p.confirm_when
                          or label_of(p, value) in p.confirm_when):
            if not self.confirm(self._template(p.confirm, value=shown)):
                return
        what = text(p.label, d) or pid
        if p.kind != "action":
            what += f" → {shown}"
            if rate is not None and p.ramp is not None:
                what += f" at {rate:g} {text(p.ramp.unit, d)}"

        def done(result):
            if p.kind != "action":
                self.values[pid] = value if result is None else result
            self.refresh()
        coro = write(defn, d, pid, value, rate)
        if p.stoppable:
            self.move(coro, what, done)
        else:
            self.call(coro, what, done)

    def stop_prop(self, pid: str) -> None:
        assert self.defn is not None
        p = self.defn.properties[pid]
        self.stop_move()
        if p.ramp is not None and p.ramp.stop:
            self.call(run(self.dev, p.ramp.stop), "hold", lambda _: self.refresh())

    def switch_mode(self, target: str | None = None) -> None:
        """Change the instrument's mode through the driver, then rebuild the panel."""
        defn = self.defn
        if defn is None or not defn.switch or not defn.mode_attr:
            return
        others = [m for m in defn.modes if m != defn.mode]
        if target is None:
            if len(others) == 1:
                target = others[0]
            else:
                label, ok = QtWidgets.QInputDialog.getItem(
                    self, f"{self.name}: switch mode", "Mode",
                    [defn.modes[m] for m in others], 0, False)
                if not ok:
                    return
                target = others[[defn.modes[m] for m in others].index(label)]
        settings: dict[str, Any] = {}
        if defn.switch_settings:
            saved = getattr(self.cfg, "modes", {}).get(target)
            if saved is None:
                self.station.log("error", f"{self.name}: no settings for {target}: add "
                                 f"[devices.{self.name}.modes.{target}] with "
                                 f"{', '.join(defn.switch_settings)} to the station file")
                return
            settings = {k: getattr(saved, k) for k in defn.switch_settings}
        if defn.switch_confirm and not self.confirm(self._template(
                defn.switch_confirm, mode=target, mode_label=defn.modes[target])):
            return

        def done(_):
            self.cfg = self.cfg.model_copy(update={defn.mode_attr: target, **settings})
            self.station.log("info", f"{self.name}: now {defn.modes[target]} (until the GUI "
                                     "closes; the station file is unchanged)")
            self.rows.clear()
            self.call(self.attach(), "read present state", self._reattached)
        self.call(run(self.dev, defn.switch, target, **settings),
                  f"switch to {defn.modes[target]}", done)

    def _reattached(self, result):
        self.after_attach(result)
        self._history.clear()                   # the units may have changed
        self.build_charts()
        self.connected.emit(self.name)          # the hub picks up the new units
        self.poll()


def detail_actions(parent: QtWidgets.QWidget, bar: QtWidgets.QToolBar, panels: list[Panel],
                   set_detail) -> dict[int, QtGui.QAction]:
    """Essential / Expanded / All toolbar buttons, which open or close the folds of
    every panel; none when no panel has folds."""
    actions: dict[int, QtGui.QAction] = {}
    if not any(p.FOLDS for p in panels):
        return actions
    bar.addSeparator()
    group = QtGui.QActionGroup(parent)
    for level, label in LEVELS.items():
        act = QtGui.QAction(label, parent, checkable=True)
        act.setToolTip(f"Show {label.lower()} properties on every panel here "
                       "(each panel's More / All folds still open on their own)")
        act.triggered.connect(lambda _=False, level=level: set_detail(level))
        group.addAction(act)
        bar.addAction(act)
        actions[level] = act
    actions[0].setChecked(True)
    return actions


# Panels in this module. The cryostat instruments (temperature, magnet, helium)
# have their own modules in common/dedicated_panels/; common/registry.py combines them.
PANELS: dict[str, type[Panel]] = {
    "keithley_2400": DefinitionPanel,
    "keithley_2401": DefinitionPanel,
    "keithley_2410": DefinitionPanel,
    "keithley_2450": DefinitionPanel,
    "keithley_2000": DefinitionPanel,
    "gigatronics_2420b": DefinitionPanel,
    "sr830": DefinitionPanel,
    "sr850": DefinitionPanel,
}
