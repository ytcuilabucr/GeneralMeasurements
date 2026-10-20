"""Monitor and chart windows: read-only views of the DataHub.

A view never polls, writes or opens a session: it only listens to the hub, so
any number of them can show the same instrument without extra traffic.

- Monitor: each chosen field as a big number with its units, coloured amber or
  red outside optional thresholds; "—" while its instrument is disconnected.
- Chart: the chosen fields on one wall-clock TimeChart; a second unit gets the
  right-hand axis (at most two units per chart).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import PySide6QtAds as ads
from PySide6 import QtCore, QtWidgets

from ..common.hub import DataHub, Field
from ..common.panels import fmt, parse_si
from ..common.form_types import FieldPickerForm, ViewWindowForm
from ..common.uiload import load_ui
from ..common.widgets import Card, TimeChart

KINDS = {"monitor": "Monitor", "chart": "Chart"}
ALARM_COLS = ("amber_low", "amber_high", "red_low", "red_high")
_ALARM_HEADS = ("Amber below", "Amber above", "Red below", "Red above")
_LEVEL_COLORS = {"red": "#c0392b", "amber": "#d68910"}
_CURVE_COLORS = ("#5dade2", "#e67e22", "#58d68d", "#f4d03f", "#af7ac5", "#ec7063",
                 "#48c9b0", "#dc7633")


@dataclass
class ViewField:
    key: str
    device: str
    label: str
    units: str
    amber_low: float | None = None
    amber_high: float | None = None
    red_low: float | None = None
    red_high: float | None = None

    @classmethod
    def of(cls, f: Field) -> ViewField:
        return cls(f.key, f.device, f.label, f.units)

    def level(self, v: float) -> str:
        """'red', 'amber' or '' for a value against the thresholds."""
        def out(lo, hi):
            return (lo is not None and v < lo) or (hi is not None and v > hi)
        if out(self.red_low, self.red_high):
            return "red"
        if out(self.amber_low, self.amber_high):
            return "amber"
        return ""


# -- picker ------------------------------------------------------------------------------
class FieldPicker(QtWidgets.QDialog, FieldPickerForm):
    """Choose fields (and, for a monitor, alarm thresholds) from the hub's list
    (field_picker.ui)."""

    def __init__(self, kind: str, fields: list[Field], chosen: list[ViewField] = (),
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        load_ui("field_picker", self)
        self.kind = kind
        self.setWindowTitle(f"{KINDS[kind]}: choose fields")
        self.resize(720 if kind == "monitor" else 460, 420)
        picked = {f.key: f for f in chosen}
        self.rows = [ViewField.of(f) for f in fields]
        self.rows += [f for f in chosen if f.key not in {r.key for r in self.rows}]
        heads = ["Field", "Units"] + (list(_ALARM_HEADS) if kind == "monitor" else [])
        self.table.setRowCount(len(self.rows))
        self.table.setColumnCount(len(heads))
        self.table.setHorizontalHeaderLabels(heads)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        flags = QtCore.Qt.ItemFlag
        for r, f in enumerate(self.rows):
            item = QtWidgets.QTableWidgetItem(f"{f.label}   [{f.key}]")
            item.setFlags(flags.ItemIsUserCheckable | flags.ItemIsEnabled)
            item.setCheckState(QtCore.Qt.CheckState.Checked if f.key in picked
                               else QtCore.Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, item)
            units = QtWidgets.QTableWidgetItem(f.units)
            units.setFlags(flags.ItemIsEnabled)
            self.table.setItem(r, 1, units)
            if kind == "monitor":
                src = picked.get(f.key, f)
                for c, attr in enumerate(ALARM_COLS, start=2):
                    v = getattr(src, attr)
                    self.table.setItem(r, c, QtWidgets.QTableWidgetItem("" if v is None
                                                                        else f"{v:g}"))
        self.hint.setText("Tick the fields to show." + (
            " Thresholds are optional; leave blank for none." if kind == "monitor" else
            " At most two different units per chart."))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def check(self, key: str, on: bool = True) -> None:
        r = next(i for i, f in enumerate(self.rows) if f.key == key)
        self.table.item(r, 0).setCheckState(QtCore.Qt.CheckState.Checked if on
                                            else QtCore.Qt.CheckState.Unchecked)

    def set_alarm(self, key: str, attr: str, text: str) -> None:
        r = next(i for i, f in enumerate(self.rows) if f.key == key)
        self.table.item(r, 2 + ALARM_COLS.index(attr)).setText(text)

    def result_fields(self) -> list[ViewField]:
        """The ticked fields with their thresholds. Raises ValueError on bad input."""
        out = []
        for r, f in enumerate(self.rows):
            if self.table.item(r, 0).checkState() != QtCore.Qt.CheckState.Checked:
                continue
            vf = ViewField(f.key, f.device, f.label, f.units)
            if self.kind == "monitor":
                for c, attr in enumerate(ALARM_COLS, start=2):
                    text = self.table.item(r, c).text().strip()
                    if text:
                        try:
                            setattr(vf, attr, parse_si(text))
                        except ValueError:
                            raise ValueError(f"{f.key}: {_ALARM_HEADS[c - 2]}: not a "
                                             f"number: {text!r}") from None
            out.append(vf)
        if not out:
            raise ValueError("tick at least one field")
        if self.kind == "chart" and len({f.units for f in out}) > 2:
            raise ValueError("a chart takes at most two different units (two y-axes)")
        return out

    def accept(self) -> None:
        try:
            self.result_fields()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Fields", str(e))
            return
        super().accept()


# -- views ---------------------------------------------------------------------------------
class MonitorView(QtWidgets.QWidget):
    def __init__(self, fields: list[ViewField], hub: DataHub):
        super().__init__()
        self.fields, self.hub = {f.key: f for f in fields}, hub
        grid = QtWidgets.QGridLayout(self)
        cols = 1 if len(fields) <= 2 else 2
        self.cards: dict[str, Card] = {}
        self._base: dict[str, str] = {}
        for i, f in enumerate(fields):
            key = "" if f.label == f.key else f" &nbsp; <span style='color:gray'>{f.key}</span>"
            c = Card(f"<b>{f.label}</b>{key}", f.units, size=36)
            self.cards[f.key] = c
            self._base[f.key] = c.value.styleSheet()
            grid.addWidget(c, i // cols, i % cols)
        grid.setRowStretch(grid.rowCount(), 1)
        hub.updated.connect(self._update)
        hub.device_changed.connect(self._device)
        for f in fields:
            last = hub.latest(f.key)
            if last is not None:
                self._update(f.key, *last)
            else:
                self._blank(f.key, "waiting" if hub.is_up(f.device) else "disconnected")

    def detach(self) -> None:
        self.hub.updated.disconnect(self._update)
        self.hub.device_changed.disconnect(self._device)

    def _blank(self, key: str, why: str) -> None:
        self.cards[key].value.setStyleSheet(self._base[key])
        self.cards[key].set("—", why)

    def _update(self, key: str, t: float, v: float) -> None:
        f = self.fields.get(key)
        if f is None:
            return
        c = self.cards[key]
        color = _LEVEL_COLORS.get(f.level(v))
        c.value.setStyleSheet(self._base[key] + (f" color: {color};" if color else ""))
        c.set(fmt(v, f.units), f"updated {time.strftime('%H:%M:%S', time.localtime(t))}")

    def _device(self, name: str, up: bool) -> None:
        for key, f in self.fields.items():
            if f.device == name:
                self._blank(key, "waiting" if up else "disconnected")

    def value_text(self, key: str) -> str:
        return self.cards[key].value.text()


class ChartView(TimeChart):
    def __init__(self, fields: list[ViewField], hub: DataHub):
        units = list(dict.fromkeys(f.units for f in fields))
        def axis_label(u):
            return ", ".join(f.label for f in fields if f.units == u)[:60]
        super().__init__(axis_label(units[0]), units[0], {})
        self.fields, self.hub = {f.key: f for f in fields}, hub
        for i, f in enumerate(fields):
            self.add_curve(f.key, _CURVE_COLORS[i % len(_CURVE_COLORS)],
                           f"{f.label} ({f.units})", right=f.units != units[0])
        if len(units) > 1:
            self.set_right_label(axis_label(units[1]), units[1])
        hub.updated.connect(self._update)
        for f in fields:
            last = hub.latest(f.key)
            if last is not None:
                self._update(f.key, *last)

    def detach(self) -> None:
        self.hub.updated.disconnect(self._update)

    def _update(self, key: str, t: float, v: float) -> None:
        if key in self.fields:
            self.add(key, v, t)                # a disconnected device just stops adding


# -- window ----------------------------------------------------------------------------------
class ViewWindow(QtWidgets.QMainWindow, ViewWindowForm):
    """A monitor or chart in its own window (one QtAds dock, so it floats and its
    layout is saved like the instrument windows'). Closing it deletes it."""

    def __init__(self, kind: str, title: str, fields: list[ViewField], station):
        super().__init__()
        load_ui("view_window", self)            # toolbar `tools`: Fields…, Station
        self.kind, self.title, self.station = kind, title, station
        self.setObjectName(f"view_{title}")
        self.setWindowTitle(f"{title} — {station.cfg.station.name}")
        self.resize(640 if kind == "monitor" else 860, 420 if kind == "monitor" else 520)
        self.actionFields.triggered.connect(self.edit_fields)
        self.actionStation.triggered.connect(station.show_raise)
        self.docks = ads.CDockManager(self)     # replaces the form's central widget
        self.dock = ads.CDockWidget(self.docks, title)
        self.dock.setObjectName("view")
        self.dock.setFeature(ads.CDockWidget.DockWidgetFeature.DockWidgetClosable, False)
        self._holder = QtWidgets.QWidget()
        self._holder_lay = QtWidgets.QVBoxLayout(self._holder)
        self._holder_lay.setContentsMargins(0, 0, 0, 0)
        self.dock.setWidget(self._holder)
        self.docks.addDockWidget(ads.DockWidgetArea.CenterDockWidgetArea, self.dock)
        self.default_layout = self.docks.saveState()
        self.view: MonitorView | ChartView | None = None
        self.set_fields(fields)

    def set_fields(self, fields: list[ViewField]) -> None:
        if self.view is not None:
            self.view.detach()
            self._holder_lay.removeWidget(self.view)
            self.view.deleteLater()
        self.fields = fields
        cls = MonitorView if self.kind == "monitor" else ChartView
        self.view = cls(fields, self.station.hub)
        self._holder_lay.addWidget(self.view)

    def edit_fields(self) -> None:
        dlg = FieldPicker(self.kind, self.station.hub.fields(), self.fields, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.set_fields(dlg.result_fields())

    def config(self) -> dict:
        return {"kind": self.kind, "title": self.title,
                "fields": [asdict(f) for f in self.fields]}

    def save_layout(self, s: QtCore.QSettings) -> None:
        s.beginGroup(f"views/{self.title}")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("docks", self.docks.saveState())
        s.endGroup()

    def restore_layout(self, s: QtCore.QSettings) -> None:
        s.beginGroup(f"views/{self.title}")
        try:
            geo, docks = s.value("geometry"), s.value("docks")
            if geo is not None:
                self.restoreGeometry(geo)
            if docks is not None:
                self.docks.restoreState(docks)
        finally:
            s.endGroup()

    def closeEvent(self, event) -> None:
        for f in self.docks.floatingWidgets():      # a floated dock is its own window
            f.hide()
        self.station.remove_view(self)
        event.accept()
