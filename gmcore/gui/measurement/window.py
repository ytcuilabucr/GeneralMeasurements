"""General Measurement: builds and runs measurements, after Labber's Measurement
Editor (docs/general-measurement.md).

    Instruments (left)        the Instrument Server's connected instruments, their
                              Set channels (steppable) and Read channels (loggable)
    Step sequence (middle)    the channels to step; the first is the inner loop
    Log channels (middle)     the readings recorded at every point
    Step channel (right)      the selected channel's step items and options
    Measurement (bottom)      name, data folder, comment, after-run action,
                              where readings come from (direct or the server's
                              register), Check / Start / Pause / Stop, a live plot

The Timer (gmcore/measurement/timer.py) is listed with the instruments: step it
in the inner loop for a time trace at exact intervals.

It runs in the server's program on the server's sessions (`server.devices`,
single owner) and its worker loop. The measurement itself is
gmcore/measurement/ (config and channels, no Qt); the run is the ordinary
runner with an HDF5 file, so data and safety behave as in `gm run`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ...api import run_scan
from ...engine import Controller
from ...engine.events import Event, Note, RunStart, RunStop
from ...measurement import (AFTER, INTERPOLATIONS, LOG_SOURCES, RANGE_TYPES, STEP_TYPES,
                            Channel, MeasurementConfig, StepChannel, StepItem, build, catalog,
                            estimate_s, timer_step)
from ...measurement import timer as tm
from ...plans import PlanError
from ...sinks.liveplot import LivePlotSink, PlotBuffer
from ...units import fmt
from ..common import icons
from ..common.form_types import GeneralMeasurementForm
from ..common.uiload import load_ui
from ..server.quick import _short

CONFIG_DIR = Path("Measurement config")
FILE_FILTER = "Measurement (*.toml);;All files (*)"
ESTIMATE_MAX_POINTS = 200_000          # above this the time is estimated on Check only
_AMBER, _RED = "#b9770e", "#c0392b"
ID = QtCore.Qt.ItemDataRole.UserRole


def duration(s: float) -> str:
    s = round(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def data_path(folder: str | Path, name: str, now: float | None = None) -> Path:
    """Where a run's data goes, as Labber files it: <folder>/YYYY/MM/Data_MMDD/
    <name>.h5, with _2, _3, ... when the name is taken."""
    t = time.localtime(now)
    day = Path(folder) / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}" / \
        f"Data_{t.tm_mon:02d}{t.tm_mday:02d}"
    stem = "".join(c if c.isalnum() or c in " -_.()" else "_" for c in name).strip() \
        or "measurement"
    path, n = day / f"{stem}.h5", 1
    while path.exists():
        n += 1
        path = day / f"{stem}_{n}.h5"
    return path


class _RunEvents(QtCore.QObject):
    """The run's bus, delivered on the Qt thread."""
    item = QtCore.Signal(object)

    async def consume(self, sub) -> None:
        async for item in sub:
            self.item.emit(item)


class GeneralMeasurementWindow(QtWidgets.QMainWindow, GeneralMeasurementForm):
    def __init__(self, server):
        super().__init__()
        self.server = server
        self.cfg = MeasurementConfig()
        self.path: Path | None = None
        self._dirty = False
        self._loading = False                       # filling widgets: ignore their signals
        self.channels: dict[str, Channel] = {}      # the catalog of connected instruments
        self.controller: Controller | None = None
        self.future = None
        self.run_path: Path | None = None
        self.buffer = PlotBuffer()
        self._total = 0
        self._plot_due = False

        load_ui("general_measurement", self)
        icons.set_window_icon(self, "measurement")
        for combo, table in ((self.range_type, RANGE_TYPES), (self.step_type, STEP_TYPES),
                             (self.interpolation, INTERPOLATIONS), (self.after, AFTER)):
            for key, label in table.items():
                combo.addItem(label, key)
        for key, label in LOG_SOURCES.items():          # "Auto: …": the part before ":"
            self.log_source.addItem(label.split(":")[0], key)
            self.log_source.setItemData(self.log_source.count() - 1, label,
                                        QtCore.Qt.ItemDataRole.ToolTipRole)
        self.data_dir.setText(self.cfg.data_dir)
        self.split_h.setSizes([300, 520, 360])
        self.split_v.setSizes([520, 300])
        self.curve = self.live_plot.plot([], [], pen=None, symbol="o", symbolSize=5)
        self.line = self.live_plot.plot([], [])
        self.live_plot.showGrid(x=True, y=True, alpha=0.3)

        # instruments
        self.instruments.itemDoubleClicked.connect(lambda it, _c: self._add_from_tree(it))
        self.btn_add_step.clicked.connect(lambda: self._add_from_tree(kind="step"))
        self.btn_add_log.clicked.connect(lambda: self._add_from_tree(kind="log"))
        # step sequence and log channels
        self.step_list.currentItemChanged.connect(lambda *_: self._show_channel())
        self.btn_step_remove.clicked.connect(self.remove_step)
        self.btn_step_up.clicked.connect(lambda: self.move_step(-1))
        self.btn_step_down.clicked.connect(lambda: self.move_step(1))
        self.btn_log_remove.clicked.connect(self.remove_log)
        self.btn_log_up.clicked.connect(lambda: self.move_log(-1))
        self.btn_log_down.clicked.connect(lambda: self.move_log(1))
        # step items
        self.item_list.currentRowChanged.connect(lambda _r: self._show_item())
        self.btn_item_add.clicked.connect(self.add_item)
        self.btn_item_remove.clicked.connect(self.remove_item)
        for w, field in ((self.item_start, "start"), (self.item_stop, "stop"),
                         (self.item_center, "center"), (self.item_span, "span"),
                         (self.item_step, "step")):
            w.editingFinished.connect(lambda f=field, w=w: self._number_edited(f, w))
        self.n_points.valueChanged.connect(lambda v: self._item_changed(n_points=v))
        for combo, field in ((self.range_type, "range_type"), (self.step_type, "step_type"),
                             (self.interpolation, "interpolation")):
            combo.currentIndexChanged.connect(
                lambda _i, c=combo, f=field: self._item_changed(**{f: c.currentData()}))
        self.settle.valueChanged.connect(lambda v: self._channel_changed(settle_s=v))
        self.snake.toggled.connect(lambda on: self._channel_changed(snake=on))
        # measurement
        self.meas_name.textEdited.connect(lambda t: self._meta_changed(name=t))
        self.comment.textEdited.connect(lambda t: self._meta_changed(comment=t))
        self.data_dir.textEdited.connect(lambda t: self._meta_changed(data_dir=t))
        self.after.currentIndexChanged.connect(
            lambda _i: self._meta_changed(after=self.after.currentData()))
        self.log_source.currentIndexChanged.connect(
            lambda _i: self._meta_changed(log_source=self.log_source.currentData()))
        self.btn_browse.clicked.connect(self._browse)
        self.btn_dry_run.clicked.connect(self.check)
        self.actionDryRun.triggered.connect(self.check)
        self.btn_run.clicked.connect(self.start)
        self.actionRun.triggered.connect(self.start)
        self.btn_pause.toggled.connect(self.pause)
        self.actionPause.triggered.connect(self.btn_pause.toggle)
        self.btn_abort.clicked.connect(self.stop)
        self.actionAbort.triggered.connect(self.stop)
        self.plot_y.currentIndexChanged.connect(lambda _i: self._redraw())
        # file
        self.actionNew.triggered.connect(self.new)
        self.actionOpenSequence.triggered.connect(self.open_dialog)
        self.actionSaveSequence.triggered.connect(self.save)
        self.actionSaveSequenceAs.triggered.connect(self.save_as)
        self.actionCloseWindow.triggered.connect(self.close)

        self.events = _RunEvents(self)
        self.events.item.connect(self._on_run_item, QtCore.Qt.ConnectionType.QueuedConnection)
        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setInterval(250)
        self.plot_timer.timeout.connect(self._redraw_if_due)
        if hasattr(server, "instruments_changed"):
            server.instruments_changed.connect(self.refresh_instruments)
        self.refresh_instruments()
        self._show_config()

    # -- instruments ---------------------------------------------------------------------
    def refresh_instruments(self) -> None:
        """The tree of connected instruments and their channels; also re-labels the
        lists (a channel of a disconnected instrument is marked)."""
        self.channels = catalog(self.server.devices)
        self.instruments.clear()
        for r in self.server.rows:
            top = QtWidgets.QTreeWidgetItem([r.name, ""])
            top.setIcon(0, icons.for_driver(r.driver))
            self.instruments.addTopLevelItem(top)
            mine = [c for c in self.channels.values() if c.instrument == r.name]
            if not mine:
                top.setText(1, "not connected")
                top.setForeground(0, self.palette().placeholderText())
                continue
            self._add_channels(top, r.name, mine)
        if tm.NAME not in self.server.devices:         # the built-in Timer, always there
            top = QtWidgets.QTreeWidgetItem([tm.NAME, "built in"])
            top.setIcon(0, icons.icon("timer"))
            top.setToolTip(0, "Step it in the inner loop (from 0) for a time trace at exact "
                              "intervals; log its clock for the real time of each point")
            self.instruments.addTopLevelItem(top)
            self._add_channels(top, tm.NAME,
                               [c for c in self.channels.values() if c.instrument == tm.NAME])
        self.instruments.expandAll()
        self.instruments.resizeColumnToContents(0)
        self._fill_lists()
        self._update_estimate()

    def _add_channels(self, top: QtWidgets.QTreeWidgetItem, name: str,
                      mine: list[Channel]) -> None:
        for kind, title, role in (("step", "Set", "sweep"), ("log", "Read", "read")):
            these = [c for c in mine if c.kind == kind]
            if not these:
                continue
            group = QtWidgets.QTreeWidgetItem(top, [title, ""])
            group.setIcon(0, icons.icon(role))
            for c in these:
                it = QtWidgets.QTreeWidgetItem(group, [_short(c.label, name), c.unit])
                it.setData(0, ID, c.id)
                it.setToolTip(0, self._channel_tip(c))

    def _channel_tip(self, c: Channel) -> str:
        lo, hi = c.limits
        lim = ("" if c.kind == "log" or (lo is None and hi is None) else
               f"; limits {fmt(lo, c.unit)} … {fmt(hi, c.unit)}")
        return f"{c.id}{lim}"

    def _add_from_tree(self, item: QtWidgets.QTreeWidgetItem | None = None,
                       kind: str | None = None) -> None:
        item = item or self.instruments.currentItem()
        cid = item.data(0, ID) if item is not None else None
        ch = self.channels.get(cid) if cid else None
        if ch is None or (kind is not None and ch.kind != kind):
            self._say(f"Select a {'Set' if kind == 'step' else 'Read'} channel first"
                      if kind else "")
            return
        (self.add_step if ch.kind == "step" else self.add_log)(ch.id)

    def label(self, cid: str) -> str:
        c = self.channels.get(cid)
        if c is None:
            return f"{cid}  (not connected)"
        return f"{c.instrument}: {_short(c.label, c.instrument)}"

    # -- step sequence and log channels --------------------------------------------------
    def add_step(self, cid: str) -> None:
        if any(sc.channel == cid for sc in self.cfg.step):
            self._say(f"{cid} is in the step sequence already")
            return
        c = self.channels.get(cid)
        lo, hi = c.limits if c else (None, None)
        start = min(max(0.0, lo if lo is not None else 0.0), hi if hi is not None else 0.0)
        self.cfg.step.append(StepChannel(cid, [StepItem("single", start=start)]))
        self._changed()
        self.step_list.setCurrentItem(self.step_list.topLevelItem(len(self.cfg.step) - 1))

    def add_log(self, cid: str) -> None:
        if cid in self.cfg.log:
            self._say(f"{cid} is logged already")
            return
        self.cfg.log.append(cid)
        self._changed()
        self.log_list.setCurrentRow(len(self.cfg.log) - 1)

    def _step_index(self) -> int:
        it = self.step_list.currentItem()
        return self.step_list.indexOfTopLevelItem(it) if it is not None else -1

    def remove_step(self) -> None:
        i = self._step_index()
        if i >= 0:
            del self.cfg.step[i]
            self._changed()

    def move_step(self, d: int) -> None:
        i = self._step_index()
        if 0 <= i and 0 <= i + d < len(self.cfg.step):
            s = self.cfg.step
            s[i], s[i + d] = s[i + d], s[i]
            self._changed()
            self.step_list.setCurrentItem(self.step_list.topLevelItem(i + d))

    def remove_log(self) -> None:
        i = self.log_list.currentRow()
        if i >= 0:
            del self.cfg.log[i]
            self._changed()

    def move_log(self, d: int) -> None:
        i = self.log_list.currentRow()
        if 0 <= i and 0 <= i + d < len(self.cfg.log):
            g = self.cfg.log
            g[i], g[i + d] = g[i + d], g[i]
            self._changed()
            self.log_list.setCurrentRow(i + d)

    def _summary(self, sc: StepChannel) -> tuple[str, str]:
        unit = self.channels[sc.channel].unit if sc.channel in self.channels else ""
        try:
            v = sc.values()
        except ValueError as e:
            return f"error: {e}", ""
        if len(v) == 1:
            return f"{fmt(v[0], unit)} (set once)", "1"
        return f"{fmt(v[0], unit)} → {fmt(v[-1], unit)}", str(len(v))

    def _fill_lists(self) -> None:
        """The two lists from the config, keeping the selections."""
        si, li = self._step_index(), self.log_list.currentRow()
        self._loading = True
        try:
            self.step_list.clear()
            for sc in self.cfg.step:
                values, n = self._summary(sc)
                it = QtWidgets.QTreeWidgetItem([self.label(sc.channel), values, n])
                it.setToolTip(0, sc.channel)
                if sc.channel not in self.channels:
                    it.setForeground(0, QtGui.QColor(_AMBER))
                self.step_list.addTopLevelItem(it)
            for c in range(3):
                self.step_list.resizeColumnToContents(c)
            self.log_list.clear()
            for key in self.cfg.log:
                c = self.channels.get(key)
                it = QtWidgets.QListWidgetItem(self.label(key) + (f" ({c.unit})"
                                                                  if c and c.unit else ""))
                it.setToolTip(key)
                if c is None:
                    it.setForeground(QtGui.QColor(_AMBER))
                self.log_list.addItem(it)
        finally:
            self._loading = False
        if 0 <= si < len(self.cfg.step):
            self.step_list.setCurrentItem(self.step_list.topLevelItem(si))
        elif self.cfg.step:
            self.step_list.setCurrentItem(self.step_list.topLevelItem(len(self.cfg.step) - 1))
        if 0 <= li < len(self.cfg.log):
            self.log_list.setCurrentRow(li)
        self._show_channel()

    # -- the selected step channel -------------------------------------------------------
    def current_channel(self) -> StepChannel | None:
        i = self._step_index()
        return self.cfg.step[i] if 0 <= i < len(self.cfg.step) else None

    def current_item(self) -> StepItem | None:
        sc = self.current_channel()
        j = self.item_list.currentRow()
        return sc.items[j] if sc is not None and 0 <= j < len(sc.items) else None

    def _show_channel(self) -> None:
        sc = self.current_channel()
        self.channel_box.setEnabled(sc is not None and not self.running)
        self._loading = True
        try:
            j = self.item_list.currentRow()
            self.item_list.clear()
            if sc is None:
                self.channel_info.setText("Select a channel in the step sequence.")
                return
            c = self.channels.get(sc.channel)
            self.channel_info.setText(f"<b>{self.label(sc.channel)}</b><br>" + (
                self._channel_tip(c) if c else "the instrument is not connected"))
            for it in sc.items:
                self.item_list.addItem(self._item_text(it, c.unit if c else ""))
            self.settle.setValue(sc.settle_s)
            self.snake.setChecked(sc.snake)
            for w in (self.item_start, self.item_stop, self.item_center, self.item_span,
                      self.item_step):
                w.unit = c.unit if c else ""
        finally:
            self._loading = False
        self.item_list.setCurrentRow(min(max(j, 0), len(sc.items) - 1))
        self._show_item()

    @staticmethod
    def _item_text(it: StepItem, unit: str) -> str:
        if it.range_type == "single":
            return f"Single: {fmt(it.start, unit)}"
        try:
            n = it.points()
        except ValueError as e:
            return f"error: {e}"
        how = " log" if it.interpolation == "log" else ""
        return f"{fmt(it.start, unit)} → {fmt(it.stop, unit)}, {n} points{how}"

    def _show_item(self) -> None:
        it, ch = self.current_item(), self.current_channel()
        self.btn_item_remove.setEnabled(it is not None and ch is not None and len(ch.items) > 1)
        for w in (self.range_type, self.item_start, self.item_stop, self.item_center,
                  self.item_span, self.step_type, self.n_points, self.item_step,
                  self.interpolation):
            w.setEnabled(it is not None)
        if it is None or ch is None:
            return
        c = self.channels.get(ch.channel)
        unit = c.unit if c else ""
        self._loading = True
        try:
            for combo, key in ((self.range_type, it.range_type), (self.step_type, it.step_type),
                               (self.interpolation, it.interpolation)):
                combo.setCurrentIndex(combo.findData(key))
            for w, v in ((self.item_start, it.start), (self.item_stop, it.stop),
                         (self.item_center, it.center), (self.item_span, it.span)):
                w.setText(fmt(v, unit).replace(" ", ""))
            try:
                n = it.points()
            except ValueError:
                n = it.n_points
            self.n_points.setValue(n)
            step = abs(it.stop - it.start) / (n - 1) if n > 1 else it.step
            self.item_step.setText(fmt(step if it.step_type == "points" else it.step,
                                       unit).replace(" ", ""))
        finally:
            self._loading = False
        single = it.range_type == "single"
        rows = {self.item_start: True, self.item_stop: it.range_type == "start_stop",
                self.item_center: it.range_type == "center_span",
                self.item_span: it.range_type == "center_span", self.step_type: not single,
                self.n_points: not single, self.item_step: not single,
                self.interpolation: not single}
        for w, shown in rows.items():
            self.item_form.setRowVisible(w, shown)
        self.start_label.setText("Value" if single else "Start")
        # the step type says which of the two is given; the other is worked out
        self.n_points.setEnabled(it.step_type == "points")
        self.item_step.setEnabled(it.step_type == "step")

    def add_item(self) -> None:
        sc = self.current_channel()
        if sc is None:
            return
        last = sc.items[-1]
        sc.items.append(StepItem("start_stop", start=last.stop, stop=last.stop))
        self._changed()
        self.item_list.setCurrentRow(len(sc.items) - 1)

    def remove_item(self) -> None:
        sc, j = self.current_channel(), self.item_list.currentRow()
        if sc is not None and len(sc.items) > 1 and 0 <= j < len(sc.items):
            del sc.items[j]
            self._changed()

    def _number_edited(self, field: str, w) -> None:
        if self._loading or self.current_item() is None:
            return
        try:
            v = w.value()
        except ValueError as e:
            self._say(f"{field}: {e}")
            self._show_item()
            return
        it = self.current_item()
        if it is None:
            return
        if field in ("start", "stop"):
            self._item_changed(**{field: v})
        elif field == "center":
            it.set_center_span(v, it.span)
            self._changed()
        elif field == "span":
            it.set_center_span(it.center, v)
            self._changed()
        else:
            self._item_changed(step=v)

    def _item_changed(self, **kw: Any) -> None:
        it = self.current_item()
        if self._loading or it is None:
            return
        for k, v in kw.items():
            setattr(it, k, v)
        self._changed()

    def _channel_changed(self, **kw: Any) -> None:
        sc = self.current_channel()
        if self._loading or sc is None:
            return
        for k, v in kw.items():
            setattr(sc, k, v)
        self._changed()

    def _meta_changed(self, **kw: Any) -> None:
        if self._loading:
            return
        for k, v in kw.items():
            setattr(self.cfg, k, v)
        self._dirty = True
        self._update_title()
        if "log_source" in kw:                    # where readings come from, and the time
            self._update_estimate()

    def _changed(self) -> None:
        self._dirty = True
        self._update_title()
        self._fill_lists()
        self._update_estimate()

    # -- the whole config ----------------------------------------------------------------
    def _show_config(self) -> None:
        self._loading = True
        try:
            self.meas_name.setText(self.cfg.name)
            self.comment.setText(self.cfg.comment)
            self.data_dir.setText(self.cfg.data_dir)
            self.after.setCurrentIndex(self.after.findData(self.cfg.after))
            self.log_source.setCurrentIndex(self.log_source.findData(self.cfg.log_source))
        finally:
            self._loading = False
        self._fill_lists()
        self._update_estimate()
        self._update_title()

    def _update_title(self) -> None:
        where = self.path.name if self.path else "unsaved"
        self.setWindowTitle(f"General Measurement — {self.cfg.name} ({where})"
                            f"{' *' if self._dirty else ''}")

    def _say(self, text: str) -> None:
        if text:
            self.statusbar.showMessage(text, 8000)

    def intervals(self) -> dict[str, float | None]:
        """Each instrument's panel polling interval (s), None while not polling."""
        return {r.name: r.panel.poll_interval_s() for r in self.server.rows
                if r.panel is not None}

    def plan(self):
        """The Scan and the runner's devices for the measurement now (PlanError)."""
        return build(self.cfg, self.server.devices, getattr(self.server, "register", None),
                     self.intervals())

    def read_time(self, sources: dict[str, str] | None = None) -> float:
        """The slowest directly read instrument's read, from its panel (reads run
        together); sampling the register costs nothing."""
        names = {self.channels[k].instrument for k in self.cfg.log if k in self.channels}
        names -= {n for n, how in (sources or {}).items() if how == "register"}
        times = [r.panel.read_time_s() for r in self.server.rows
                 if r.name in names and r.panel is not None]
        return max((t for t in times if t is not None), default=0.0)

    def _sources_text(self, scan) -> str:
        """Where each logged instrument's readings come from, in words."""
        sources = scan.metadata.get("log_sources", {})
        polls = scan.metadata.get("server_intervals_s", {})
        parts = []
        for inst, how in sources.items():
            if how == "register":
                parts.append(f"{inst}: server register (panel reads every "
                             f"{fmt(polls.get(inst), 's')})")
            else:
                parts.append(f"{inst}: read directly")
        return "; ".join(parts)

    def _estimate(self, scan) -> float:
        return estimate_s(scan, read_s=self.read_time(scan.metadata.get("log_sources")))

    def _update_estimate(self, full: bool = False) -> tuple[bool, str]:
        """Points and time, or why the measurement can't run; shown next to Start."""
        try:
            scan, _ = self.plan()
        except (PlanError, ValueError) as e:
            self.estimate.setText(f"<span style='color:{_AMBER}'>{e}</span>")
            self.log_source_info.setText("")
            return False, str(e)
        self.log_source_info.setText(self._sources_text(scan))
        n = scan.n_points
        text = f"{n} point{'s' if n != 1 else ''}"
        if full or n <= ESTIMATE_MAX_POINTS:
            text += f", about {duration(self._estimate(scan))}"
        late = self._late_warning(scan)
        self.estimate.setText(text + (f" <span style='color:{_AMBER}'>— {late}</span>"
                                      if late else ""))
        return True, text + (f" — {late}" if late else "")

    def _late_warning(self, scan) -> str:
        """The Timer's step against the slowest direct read: points that can't
        keep up fall behind the grid."""
        dt = timer_step(self.cfg)
        read_s = self.read_time(scan.metadata.get("log_sources"))
        if dt is None or read_s < dt:
            return ""
        return (f"reads take {fmt(read_s, 's')}, not less than the Timer step "
                f"{fmt(dt, 's')}: points will fall behind (sample the server register, "
                "or lengthen the step)")

    # -- file ------------------------------------------------------------------------------
    def new(self) -> None:
        if not self.running and self._maybe_save():
            self.cfg, self.path, self._dirty = MeasurementConfig(), None, False
            self._show_config()

    def open_dialog(self) -> None:
        if self.running or not self._maybe_save():
            return
        start = str(self.path.parent if self.path else CONFIG_DIR)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open measurement", start,
                                                        FILE_FILTER)
        if path:
            self.load(path)

    def load(self, path: str | Path) -> bool:
        path = Path(path)
        try:
            cfg = MeasurementConfig.from_toml(path.read_text("utf-8"))
        except (OSError, ValueError, TypeError) as e:
            QtWidgets.QMessageBox.warning(self, "Open measurement", f"{path}: {e}")
            return False
        self.cfg, self.path, self._dirty = cfg, path, False
        self._show_config()
        self._say(f"opened {path}")
        return True

    def save(self) -> bool:
        return self.save_as() if self.path is None else self._write(self.path)

    def save_as(self) -> bool:
        start = str(self.path or CONFIG_DIR / f"{self.cfg.name}.toml")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save measurement", start,
                                                        FILE_FILTER)
        return bool(path) and self._write(Path(path))

    def _write(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.cfg.to_toml(), "utf-8")
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Save measurement", f"{path}: {e}")
            return False
        self.path, self._dirty = path, False
        self._update_title()
        self._say(f"saved {path}")
        return True

    def _maybe_save(self) -> bool:
        if not self._dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", "Save the changes to the measurement?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel)
        if r == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save()
        return r == QtWidgets.QMessageBox.StandardButton.Discard

    def _browse(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Data folder", self.cfg.data_dir)
        if d:
            self.data_dir.setText(d)
            self._meta_changed(data_dir=d)

    # -- run -------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.future is not None

    def check(self) -> bool:
        """Every point against the limits, and the time; sets nothing."""
        ok, text = self._update_estimate(full=True)
        self._say(f"Check: {'OK, ' if ok else ''}{text}")
        return ok

    def start(self) -> bool:
        if self.running:
            return False
        try:
            scan, devices = self.plan()
        except (PlanError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Start", str(e))
            return False
        path = data_path(self.cfg.data_dir, self.cfg.name)
        stepped = "\n".join(f"  {self.label(sc.channel)}: {self._summary(sc)[0]}"
                            for sc in self.cfg.step)
        readings = self._sources_text(scan)
        r = QtWidgets.QMessageBox.question(
            self, "Start measurement",
            f"Start “{self.cfg.name}”: {scan.n_points} points, about "
            f"{duration(self._estimate(scan))}.\n\n"
            f"These will be set:\n{stepped}\n\n"
            + (f"Readings: {readings}.\n" if readings else "")
            + f"After the run: {AFTER[self.cfg.after]}.\nData: {path}")
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
        self.controller = Controller()
        self.run_path, self._total = path, scan.n_points
        self.buffer = PlotBuffer()
        self.progress.setRange(0, scan.n_points)
        self.progress.setValue(0)
        self._set_running(True)
        self.server.log("info", f"measurement {self.cfg.name}: started, data to {path}")
        provenance = {"measurement": self.cfg.to_toml(), "comment": self.cfg.comment,
                      "name": self.cfg.name}
        self.future = self.server.worker.submit(
            run_scan(scan, devices, out=path, shutdown_order=self.server.shutdown_order(),
                     controller=self.controller, provenance=provenance,
                     sinks=[("gui", self.events.consume)]),
            self._finished, self._failed)
        return True

    def pause(self, on: bool) -> None:
        if self.controller is None:
            return
        fn = self.controller.pause if on else self.controller.resume
        self.server.worker.loop.call_soon_threadsafe(fn)
        self.btn_pause.setText("Resume" if on else "Pause")
        self._say("paused: the instruments hold their values" if on else "resumed")

    def stop(self) -> None:
        if self.controller is not None:
            self.server.worker.loop.call_soon_threadsafe(self.controller.abort,
                                                         "stopped by the user")

    def _set_running(self, on: bool) -> None:
        for w in (self.instruments_box, self.lists, self.channel_box, self.meas_name,
                  self.comment, self.data_dir, self.btn_browse, self.after,
                  self.log_source, self.btn_run,
                  self.btn_dry_run):
            w.setEnabled(not on)
        for a in (self.actionNew, self.actionOpenSequence, self.actionRun, self.actionDryRun):
            a.setEnabled(not on)
        self.btn_pause.setEnabled(on)
        self.btn_abort.setEnabled(on)
        self.actionPause.setEnabled(on)
        self.actionAbort.setEnabled(on)
        if not on:
            self.btn_pause.setChecked(False)
            self.btn_pause.setText("Pause")
            self._show_channel()
        if on:
            self.plot_timer.start()
        else:
            self.plot_timer.stop()

    def _on_run_item(self, item) -> None:
        if isinstance(item, RunStart):
            self.buffer.start(LivePlotSink._meta(item))
            keep = self.plot_y.currentData()
            self.plot_y.clear()
            for key in self.cfg.log:
                if key in self.buffer.units:
                    self.plot_y.addItem(self.label(key), key)
            i = self.plot_y.findData(keep)
            self.plot_y.setCurrentIndex(max(i, 0))
            x = self.buffer.axis_names[-1] if self.buffer.axis_names else ""
            c = self.channels.get(x)
            self.live_plot.setLabel("bottom", self.label(x) if x else "",
                                    units=c.unit if c else None)
        elif isinstance(item, Event):
            self.buffer.add(item.index, item.setpoints, item.data)
            self.progress.setValue(self.buffer.n)
            self._plot_due = True
        elif isinstance(item, Note):
            self.server.log("warn", f"measurement {self.cfg.name}: {item.message}")
        elif isinstance(item, RunStop):
            self.statusbar.showMessage(f"{item.status}" + (f": {item.reason}"
                                                           if item.reason else ""))

    def _redraw_if_due(self) -> None:
        if self._plot_due:
            self._plot_due = False
            self._redraw()

    def _redraw(self) -> None:
        key = self.plot_y.currentData()
        if (not key or not self.buffer.n or not self.buffer.axis_names
                or key not in self.buffer.units):           # nothing measured yet
            self.curve.setData([], [])
            self.line.setData([], [])
            return
        x, y = self.buffer.series(key)
        ok = np.isfinite(y)
        self.curve.setData(x[ok], y[ok])
        self.line.setData(x[ok], y[ok])
        c = self.channels.get(key)
        self.live_plot.setLabel("left", self.label(key), units=c.unit if c else None)

    def _finished(self, stop: RunStop) -> None:
        self._end(f"{stop.status}" + (f" ({stop.reason})" if stop.reason else ""),
                  "info" if stop.status == "success" else "warn")

    def _failed(self, e: BaseException) -> None:
        self._end(f"failed: {type(e).__name__}: {e}", "error")

    def _end(self, what: str, level: str) -> None:
        self._redraw()
        self.future = self.controller = None
        self._set_running(False)
        msg = f"measurement {self.cfg.name}: {what}; data in {self.run_path}"
        self.server.log(level, msg)
        self.statusbar.showMessage(msg)

    # -- window ----------------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.refresh_instruments()
        self._set_running(self.running)
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.running:
            QtWidgets.QMessageBox.information(self, "Close", "A measurement is running: "
                                              "stop it first.")
            event.ignore()
            return
        if not self._maybe_save():
            event.ignore()
            return
        event.accept()
