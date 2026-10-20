"""The Instrument Server's Quick view: one block of readings per connected
instrument, stacked vertically, and a Settings dialog to hide some of them.

It reads nothing itself: it shows what each instrument's panel already polls
(the panel's `reading` signal), so it adds no traffic to the instruments.
Hidden readings are kept by field key ("Keithley 2400 - 1.i") in the server's
settings file, so they stay hidden across sessions and reconnects.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...devices.protocols import FieldInfo
from ...units import fmt

HIDDEN_KEY = "quick_view/hidden"


def _text(value, info: FieldInfo) -> str:
    if value is None:
        return "—"
    if not info.units and "(0/1)" in info.label:            # a status, e.g. in compliance
        return "yes" if value else "no"
    try:
        return fmt(value, info.units)
    except (TypeError, ValueError):
        return str(value)


def _short(label: str, name: str) -> str:
    """A reading's label inside its instrument's block: without the name."""
    return label[len(name):].strip() or label if label.startswith(name + " ") else label


class QuickView(QtCore.QObject):
    """Owns the blocks in the Quick view tab's layout (above its last stretch)."""

    def __init__(self, layout: QtWidgets.QVBoxLayout, empty: QtWidgets.QLabel,
                 settings: QtCore.QSettings):
        super().__init__()
        self.layout, self.empty, self.settings = layout, empty, settings
        self.hidden: set[str] = set(settings.value(HIDDEN_KEY, [], list) or [])
        self.fields: dict[str, dict[str, FieldInfo]] = {}   # instrument -> its readings
        self.blocks: dict[str, QtWidgets.QGroupBox] = {}
        self.values: dict[str, QtWidgets.QLabel] = {}       # field key -> value label
        self.latest: dict[str, object] = {}                 # field key -> last value

    # -- content ---------------------------------------------------------------------
    def set_instruments(self, fields: dict[str, dict[str, FieldInfo]]) -> None:
        """The connected instruments and their readings, in table order."""
        self.fields = fields
        self.latest = {k: v for k, v in self.latest.items()
                       if any(k in f for f in fields.values())}
        self.rebuild()

    def rebuild(self) -> None:
        for box in self.blocks.values():
            self.layout.removeWidget(box)
            box.deleteLater()
        self.blocks.clear()
        self.values.clear()
        shown = 0
        for name, fields in self.fields.items():
            keys = [k for k in fields if k not in self.hidden]
            if not keys:
                continue
            box = QtWidgets.QGroupBox(name)
            grid = QtWidgets.QGridLayout(box)
            grid.setColumnStretch(1, 1)
            for row, key in enumerate(keys):
                info = fields[key]
                grid.addWidget(QtWidgets.QLabel(_short(info.label or key, name)), row, 0)
                val = QtWidgets.QLabel(_text(self.latest.get(key), info))
                val.setStyleSheet("font-size: 18pt; font-family: Consolas, monospace;")
                val.setTextInteractionFlags(
                    QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(val, row, 1)
                self.values[key] = val
            self.layout.insertWidget(self.layout.count() - 1, box)   # above the stretch
            self.blocks[name] = box
            shown += 1
        self.empty.setVisible(shown == 0)
        self.empty.setText("No instrument connected: connect one in the Config tab."
                           if not self.fields else
                           "Every reading is hidden: choose some in Settings…")

    def update(self, name: str, _t: float, data: dict) -> None:
        """A poll from an instrument's panel."""
        fields = self.fields.get(name, {})
        for key, v in data.items():
            if key in fields:
                self.latest[key] = v
                label = self.values.get(key)
                if label is not None:
                    label.setText(_text(v, fields[key]))

    # -- hiding ------------------------------------------------------------------------
    def set_hidden(self, hidden: set[str]) -> None:
        self.hidden = set(hidden)
        self.settings.setValue(HIDDEN_KEY, sorted(self.hidden))
        self.rebuild()

    def dialog(self, parent: QtWidgets.QWidget) -> QuickViewSettings:
        return QuickViewSettings(self.fields, self.hidden, parent)

    def edit(self, parent: QtWidgets.QWidget) -> None:
        """Settings…: tick the readings to show."""
        dlg = self.dialog(parent)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            # keep the choices made for instruments that are not connected now
            others = {k for k in self.hidden if not any(k in f for f in self.fields.values())}
            self.set_hidden(others | dlg.hidden())


class QuickViewSettings(QtWidgets.QDialog):
    """A checkbox per reading of every connected instrument, grouped by instrument
    (an instrument's box ticks or clears all its readings)."""

    def __init__(self, fields: dict[str, dict[str, FieldInfo]], hidden: set[str],
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Quick view: readings to show")
        self.resize(460, 480)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("Untick a reading to hide it from the quick view."))
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        flags = QtCore.Qt.ItemFlag
        state = QtCore.Qt.CheckState
        self.items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for name, readings in fields.items():
            top = QtWidgets.QTreeWidgetItem([name])
            top.setFlags(top.flags() | flags.ItemIsUserCheckable | flags.ItemIsAutoTristate)
            self.tree.addTopLevelItem(top)
            for key, info in readings.items():
                label = _short(info.label or key, name)
                item = QtWidgets.QTreeWidgetItem(
                    top, [f"{label} ({info.units})" if info.units else label])
                item.setFlags(item.flags() | flags.ItemIsUserCheckable)
                item.setCheckState(0, state.Unchecked if key in hidden else state.Checked)
                item.setToolTip(0, key)
                self.items[key] = item
        self.tree.expandAll()
        lay.addWidget(self.tree)
        if not fields:
            lay.addWidget(QtWidgets.QLabel("No instrument is connected."))
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def set_shown(self, key: str, on: bool) -> None:
        self.items[key].setCheckState(0, QtCore.Qt.CheckState.Checked if on
                                      else QtCore.Qt.CheckState.Unchecked)

    def hidden(self) -> set[str]:
        return {k for k, item in self.items.items()
                if item.checkState(0) != QtCore.Qt.CheckState.Checked}
