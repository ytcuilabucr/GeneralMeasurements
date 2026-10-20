"""Widgets for the cryostat panels: LEDs, big readouts, a dewar level gauge, a
field bar, a wall-clock trend chart, CSV logging, and the fits behind the
'stable' and 'rate' indicators (as in the legacy LabVIEW monitors). Also the
toggle list (`Fold`) that the standard panel folds its less-used rows into."""

from __future__ import annotations

import csv
import math
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .icons import icon

LED_COLORS = {"on": "#27ae60", "off": "#7f8c8d", "warn": "#f39c12", "bad": "#c0392b",
              "busy": "#3498db"}
RANGES = {"All": None, "Last 30 min": 1800.0, "Last 2 h": 7200.0, "Last 24 h": 86400.0}


def accent_style(color: str) -> str:
    """Style sheet giving a panel its own colour: header bar, group frames, titles,
    cards. The cryostat panels carry a copy in their .ui files (top-level
    styleSheet), so Designer shows them in colour."""
    return f"""
    QLabel[panelTitle="true"] {{ background: {color}; color: white; padding: 6px 10px;
                                 border-radius: 4px; font-size: 12pt; }}
    QGroupBox {{ border: 1px solid {color}; border-radius: 6px; margin-top: 1.1em;
                 padding: 6px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                        color: {color}; font-weight: bold; }}
    QFrame[card="true"] {{ border: 1px solid {color}; border-left: 6px solid {color};
                           border-radius: 6px; background: palette(base); }}
    """


# -- fits ----------------------------------------------------------------------------
def slope_per_s(points, window_s: float | None) -> float | None:
    """Least-squares slope (units/s) of (t, y) points in the last window_s seconds."""
    if not points:
        return None
    t_end = points[-1][0]
    sel = [(t, y) for t, y in points if window_s is None or t >= t_end - window_s]
    sel = [(t, y) for t, y in sel if math.isfinite(y)]
    if len(sel) < 3 or sel[-1][0] - sel[0][0] <= 0:
        return None
    t, y = np.array(sel).T
    return float(np.polyfit(t - t[0], y, 1)[0])


def is_stable(points, window_s: float, tolerance: float) -> bool | None:
    """All points in the last window_s within ±tolerance of their mean; None if the
    history doesn't span the window yet."""
    if not points or points[-1][0] - points[0][0] < window_s:
        return None
    t_end = points[-1][0]
    ys = [y for t, y in points if t >= t_end - window_s]
    if not ys or not all(math.isfinite(y) for y in ys):
        return False
    m = sum(ys) / len(ys)
    return max(abs(y - m) for y in ys) <= tolerance


# -- small widgets ---------------------------------------------------------------------
class Led(QtWidgets.QLabel):
    """A coloured dot and a label. In Designer: promoted QLabel, property `label`."""

    def __init__(self, text: str = "", state: str = "off"):
        super().__init__()
        self._text = text
        self.set(state)

    def set(self, state: str, text: str | None = None) -> None:
        if text is not None:
            self._text = text
        self.state = state
        color = LED_COLORS.get(state, state)
        self.setText(f"<span style='color:{color}; font-size:15pt'>●</span> {self._text}")

    def _set_label(self, text: str) -> None:
        self.set(self.state, text)

    label = QtCore.Property(str, lambda self: self._text, _set_label)


class CardLabels:
    """A card laid out in Designer: a QFrame with the dynamic property card=true
    and labels <frame>_caption, <frame>_value, <frame>_sub. Same set() as Card."""

    def __init__(self, owner: object, frame: str):
        self.frame = getattr(owner, frame)
        self.caption = getattr(owner, f"{frame}_caption")
        self.value = getattr(owner, f"{frame}_value")
        self.sub = getattr(owner, f"{frame}_sub")

    def set(self, text: str, sub: str | None = None) -> None:
        self.value.setText(text)
        if sub is not None:
            self.sub.setText(sub)

    def setVisible(self, on: bool) -> None:  # noqa: N802 (like the QWidget it stands for)
        self.frame.setVisible(on)


class Card(QtWidgets.QFrame):
    """Caption, a big number with unit, and a small line underneath (built in code;
    see CardLabels for the Designer version)."""

    def __init__(self, caption: str, unit: str = "", size: int = 24):
        super().__init__()
        self.setProperty("card", True)
        self.unit = unit
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        self.caption = QtWidgets.QLabel(caption)
        self.value = QtWidgets.QLabel("—")
        self.value.setStyleSheet(f"font-size: {size}pt; font-family: Consolas, monospace;")
        self.value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sub = QtWidgets.QLabel("")
        self.sub.setStyleSheet("color: palette(mid);")
        for w in (self.caption, self.value, self.sub):
            lay.addWidget(w)
        self.extra = QtWidgets.QHBoxLayout()
        lay.addLayout(self.extra)

    def set(self, text: str, sub: str | None = None) -> None:
        self.value.setText(text)
        if sub is not None:
            self.sub.setText(sub)


class Fold(QtWidgets.QWidget):
    """A toggle list: a ▸ heading that shows or hides the form under it."""

    toggled = QtCore.Signal(bool)

    def __init__(self, title: str = "", tip: str = ""):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.button = QtWidgets.QToolButton()
        self.button.setText(title)
        self.button.setToolTip(tip)
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.button.toggled.connect(self._toggled)
        self.body = QtWidgets.QWidget()
        self.form = QtWidgets.QFormLayout(self.body)
        self.form.setContentsMargins(18, 0, 0, 0)       # indented under the arrow
        self.body.setVisible(False)
        lay.addWidget(self.button)
        lay.addWidget(self.body)
        self._fit()

    def _fit(self) -> None:
        """Closed: no taller than the heading, so a splitter gives the room to
        the open folds."""
        open_ = self.button.isChecked()
        self.setMaximumHeight(16777215 if open_ else self.button.sizeHint().height() + 2)

    def is_open(self) -> bool:
        return self.button.isChecked()

    def set_open(self, on: bool) -> None:
        self.button.setChecked(on)

    def _toggled(self, on: bool) -> None:
        self.button.setArrowType(QtCore.Qt.ArrowType.DownArrow if on
                                 else QtCore.Qt.ArrowType.RightArrow)
        self.body.setVisible(on)
        self._fit()
        self.toggled.emit(on)


class MenuPicker(QtWidgets.QToolButton):
    """A two-level drop-down: groups on the first level, each opening a sub-menu
    of items (like a combo box whose list is a menu with sub-menus). The
    present choice is ticked and shown on the button; `picked` fires when the
    user chooses another one."""

    picked = QtCore.Signal(object)

    def __init__(self, groups: dict[str, list[tuple[str, object]]] | None = None,
                 tip: str = ""):
        super().__init__()
        self.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.setToolTip(tip)
        self._menu = QtWidgets.QMenu(self)
        self.setMenu(self._menu)
        self._group = QtGui.QActionGroup(self)
        self._actions: dict[object, QtGui.QAction] = {}
        self._data: object = None
        if groups:
            self.set_groups(groups)

    def set_groups(self, groups: dict[str, list[tuple[str, object]]]) -> None:
        self._menu.clear()
        self._actions.clear()
        for title, items in groups.items():
            if not items:
                continue
            sub = self._menu.addMenu(title)
            for label, data in items:
                act = sub.addAction(label)
                act.setCheckable(True)
                self._group.addAction(act)
                act.triggered.connect(lambda _=False, d=data: self._choose(d))
                self._actions[data] = act
        self.set_current_data(self._data)

    def current_data(self) -> object:
        return self._data

    def set_current_data(self, data: object) -> None:
        """Show `data` as the choice (no signal)."""
        self._data = data
        act = self._actions.get(data)
        if act is not None:
            act.setChecked(True)
        self.setText(act.text() if act is not None else ("" if data is None else str(data)))

    def action_for(self, data: object) -> QtGui.QAction | None:
        return self._actions.get(data)

    def _choose(self, data: object) -> None:
        if data != self._data:
            self.set_current_data(data)
            self.picked.emit(data)


def num(value, digits: int = 4, unit: str = "") -> str:
    if value is None:
        return "—"
    v = float(value)
    if math.isnan(v):
        return "NaN"
    return f"{v:.{digits}f} {unit}".strip()


def duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


# -- dewar gauge -------------------------------------------------------------------------
class DewarGauge(QtWidgets.QWidget):
    """A dewar outline filled to the level, with the meter's low/high marks."""

    def __init__(self, unit: str = "in"):
        super().__init__()
        self.unit = unit
        self.level: float | None = None
        self.full = 1.0                   # level at the top of the drawing
        self.low: float | None = None
        self.high: float | None = None
        self.setMinimumSize(150, 300)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Expanding)

    def set(self, level: float | None = None, full: float | None = None,
            low: float | None = None, high: float | None = None) -> None:
        if level is not None:
            self.level = level
        if full is not None and full > 0:
            self.full = full
        if low is not None:
            self.low = low
        if high is not None:
            self.high = high
        self.update()

    def _y(self, body: QtCore.QRectF, v: float) -> float:
        f = min(max(v / self.full, 0.0), 1.0)
        return body.bottom() - f * body.height()

    def paintEvent(self, _event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        neck = QtCore.QRectF(w * 0.38, 8, w * 0.24, h * 0.12)
        body = QtCore.QRectF(w * 0.12, neck.bottom(), w * 0.76, h - neck.bottom() - 28)
        outline = QtGui.QPen(QtGui.QColor("#5d6d7e"), 3)
        p.setPen(outline)
        p.setBrush(QtGui.QColor("#eaf2f8"))
        p.drawRoundedRect(neck, 4, 4)
        p.drawRoundedRect(body, 18, 18)
        if self.level is not None and math.isfinite(self.level):
            top = self._y(body, self.level)
            liquid = QtCore.QRectF(body.left() + 3, top, body.width() - 6, body.bottom() - top - 3)
            grad = QtGui.QLinearGradient(liquid.topLeft(), liquid.bottomLeft())
            low = self.low is not None and self.level <= self.low
            grad.setColorAt(0, QtGui.QColor("#e74c3c" if low else "#76d7ea"))
            grad.setColorAt(1, QtGui.QColor("#c0392b" if low else "#1f77b4"))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(liquid, 14, 14)
        for v, color, tag in ((self.high, "#27ae60", "high"), (self.low, "#c0392b", "low")):
            if v is not None and math.isfinite(v):
                y = self._y(body, v)
                p.setPen(QtGui.QPen(QtGui.QColor(color), 2, QtCore.Qt.PenStyle.DashLine))
                p.drawLine(QtCore.QPointF(body.left() - 6, y), QtCore.QPointF(body.right() + 6, y))
                p.drawText(QtCore.QPointF(body.right() - 34, y - 3), f"{tag}")
        p.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        f = p.font()
        f.setPointSize(11)
        f.setBold(True)
        p.setFont(f)
        text = "—" if self.level is None else f"{self.level:.1f} {self.unit}"
        p.drawText(QtCore.QRectF(0, h - 24, w, 22), QtCore.Qt.AlignmentFlag.AlignCenter, text)
        p.end()


# -- field bar ---------------------------------------------------------------------------
class FieldBar(QtWidgets.QWidget):
    """Horizontal bar over the station limits: zero mark, fill from zero to the
    present value, a triangle at the target."""

    def __init__(self, unit: str = "A"):
        super().__init__()
        self.unit = unit
        self.lo, self.hi = -1.0, 1.0
        self.value: float | None = None
        self.target: float | None = None
        self.setMinimumHeight(54)

    def set(self, value: float | None = None, target: float | None = None,
            limits: tuple[float, float] | None = None) -> None:
        if value is not None:
            self.value = value
        if target is not None:
            self.target = target
        if limits is not None and limits[1] > limits[0]:
            self.lo, self.hi = limits
        self.update()

    def _x(self, track: QtCore.QRectF, v: float) -> float:
        f = (min(max(v, self.lo), self.hi) - self.lo) / (self.hi - self.lo)
        return track.left() + f * track.width()

    def paintEvent(self, _event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        track = QtCore.QRectF(12, 18, self.width() - 24, 14)
        p.setPen(QtGui.QPen(QtGui.QColor("#5d6d7e"), 1))
        p.setBrush(QtGui.QColor("#ebf5fb"))
        p.drawRoundedRect(track, 6, 6)
        zero = self._x(track, 0.0)
        if self.value is not None and math.isfinite(self.value):
            x = self._x(track, self.value)
            fill = QtCore.QRectF(min(zero, x), track.top() + 1, abs(x - zero), track.height() - 2)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#2e86c1" if self.value >= 0 else "#8e44ad"))
            p.drawRect(fill)
        p.setPen(QtGui.QPen(QtGui.QColor("#17202a"), 2))
        p.drawLine(QtCore.QPointF(zero, track.top() - 4), QtCore.QPointF(zero, track.bottom() + 4))
        if self.target is not None and math.isfinite(self.target):
            x = self._x(track, self.target)
            tri = QtGui.QPolygonF([QtCore.QPointF(x, track.top() - 1),
                                   QtCore.QPointF(x - 7, track.top() - 13),
                                   QtCore.QPointF(x + 7, track.top() - 13)])
            p.setBrush(QtGui.QColor("#e67e22"))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPolygon(tri)
        p.setPen(self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText))
        y = track.bottom() + 16
        p.drawText(QtCore.QPointF(track.left(), y), f"{self.lo:g} {self.unit}")
        r = f"{self.hi:g} {self.unit}"
        p.drawText(QtCore.QPointF(track.right() - p.fontMetrics().horizontalAdvance(r), y), r)
        p.drawText(QtCore.QPointF(zero - 3, y), "0")
        p.end()


# -- chart -------------------------------------------------------------------------------
class TimeChart(QtWidgets.QWidget):
    """Wall-clock trend of several named curves, with a display-range selector.
    In Designer: promoted QWidget, properties `yLabel`, `yUnit` and `curves`
    ("A=#e67e22; B=#5dade2")."""

    def __init__(self, y_label: str = "", unit: str = "",
                 curves: dict[str, str] | None = None, maxlen: int = 20000):
        super().__init__()
        self._y = [y_label, unit]
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Display range"))
        self.range = QtWidgets.QComboBox()
        self.range.addItems(list(RANGES))
        self.range.currentTextChanged.connect(lambda _: self._redraw())
        bar.addWidget(self.range)
        clear = QtWidgets.QPushButton("Clear")
        clear.setIcon(icon("remove"))
        clear.clicked.connect(self.clear)
        bar.addWidget(clear)
        bar.addStretch()
        lay.addLayout(bar)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setMinimumHeight(160)
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("left", y_label, unit)
        self.legend = self.plot.addLegend(offset=(10, 10))
        lay.addWidget(self.plot, stretch=1)
        self.maxlen = maxlen
        self.right: pg.ViewBox | None = None
        self.data: dict[str, deque[tuple[float, float]]] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        for key, color in (curves or {}).items():
            self.add_curve(key, color)

    # Designer properties
    def _set_y(self, i: int, text: str) -> None:
        self._y[i] = text
        self.plot.setLabel("left", *self._y)

    def _curve_text(self) -> str:
        return "; ".join(f"{k}={c.opts['pen'].color().name()}" for k, c in self.curves.items())

    def _set_curves(self, text: str) -> None:
        for part in filter(None, (p.strip() for p in text.split(";"))):
            key, _, color = part.partition("=")
            if key.strip() not in self.curves:
                self.add_curve(key.strip(), color.strip() or "#5dade2")

    yLabel = QtCore.Property(str, lambda self: self._y[0], lambda self, t: self._set_y(0, t))
    yUnit = QtCore.Property(str, lambda self: self._y[1], lambda self, t: self._set_y(1, t))
    curves_spec = QtCore.Property(str, _curve_text, _set_curves)

    def add_curve(self, key: str, color: str, label: str | None = None,
                  right: bool = False) -> None:
        """A curve on the left axis, or on the right one (a second unit)."""
        self.data[key] = deque(maxlen=self.maxlen)
        pen = pg.mkPen(color, width=2)
        if right:
            item = pg.PlotDataItem(pen=pen, name=label or key)
            self._right_box().addItem(item)
            self.legend.addItem(item, label or key)
        else:
            item = self.plot.plot(name=label or key, pen=pen)
        self.curves[key] = item

    def set_right_label(self, label: str, unit: str) -> None:
        self._right_box()
        self.plot.setLabel("right", label, unit)

    def _right_box(self) -> pg.ViewBox:
        if self.right is None:
            pi = self.plot.getPlotItem()
            self.right = pg.ViewBox()
            pi.showAxis("right")
            pi.scene().addItem(self.right)
            pi.getAxis("right").linkToView(self.right)
            self.right.setXLink(pi)
            pi.vb.sigResized.connect(self._sync_right)
            self._sync_right()
        return self.right

    def _sync_right(self) -> None:
        pi = self.plot.getPlotItem()
        self.right.setGeometry(pi.vb.sceneBoundingRect())
        self.right.linkedViewChanged(pi.vb, self.right.XAxis)

    def add(self, key: str, value: float | None, t: float | None = None) -> None:
        if value is None or not math.isfinite(value) or abs(value) >= 9e37:
            return
        self.data[key].append((time.time() if t is None else t, float(value)))
        self._redraw(key)

    def clear(self) -> None:
        for d in self.data.values():
            d.clear()
        self._redraw()

    def _redraw(self, only: str | None = None) -> None:
        window = RANGES[self.range.currentText()]
        now = time.time()
        for key in [only] if only else list(self.data):
            pts = [(t, y) for t, y in self.data[key] if window is None or t >= now - window]
            if pts:
                xs, ys = zip(*pts)
                self.curves[key].setData(xs, ys)
            else:
                self.curves[key].setData([], [])


# -- logging -----------------------------------------------------------------------------
class CsvLog(QtWidgets.QWidget):
    """'Log to CSV' row: one file per device per day, a header when created."""

    def __init__(self, name: str = "log", folder: str | Path | None = None):
        super().__init__()
        self.name = name                          # the file prefix; panels set it
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.enabled = QtWidgets.QCheckBox("Log to CSV")
        self.folder = QtWidgets.QLineEdit(str(folder or Path.home() / "GeneralMeasurements"
                                              / "logs"))
        browse = QtWidgets.QPushButton()
        browse.setIcon(icon("folder"))
        browse.setToolTip("Choose the log folder")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse)
        self.file_label = QtWidgets.QLabel("")
        for w in (self.enabled, self.folder, browse, self.file_label):
            lay.addWidget(w)

    def _browse(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Log folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def path(self) -> Path:
        return Path(self.folder.text()) / f"{self.name}_{time.strftime('%Y%m%d')}.csv"

    def write(self, row: dict) -> Path | None:
        if not self.enabled.isChecked():
            return None
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", *row])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), *row.values()])
        self.file_label.setText(p.name)
        return p
