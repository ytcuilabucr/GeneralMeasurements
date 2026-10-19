"""Load Qt Designer forms (gui/forms/*.ui) at runtime.

    load_ui("server", self)

builds gui/forms/server.ui *into* `self`: the form's top-level widget is `self`, and
every named widget, layout and action becomes an attribute (`self.btn_connect`).
Edit the .ui files in Qt Designer (pyside6-designer); changes show on the next
start, with no build step. Loading takes a few milliseconds per window.

Our own widgets (Led, TimeChart, SIEdit, ...) are "promoted" placeholders in
Designer: Designer draws their base class, the program creates the real one.
Their Designer-editable settings are Qt properties (`label`, `curves`, `unit`).

A widget name that would replace one of the class's methods or properties, or an
attribute the instance already set before loading, is
refused, so a rename in Designer can't silently break the code.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from PySide6.QtUiTools import QUiLoader

FORMS_DIR = Path(__file__).resolve().parents[1] / "forms"     # gmcore/gui/forms


@cache
def forms() -> dict[str, Path]:
    """Every form, by name (the file stem)."""
    return {p.stem: p for p in sorted(FORMS_DIR.glob("*.ui"))}


def _custom_widgets() -> dict[str, type[QtWidgets.QWidget]]:
    import pyqtgraph as pg

    from .panels import SIEdit
    from .widgets import CsvLog, DewarGauge, FieldBar, Fold, Led, TimeChart
    return {c.__name__: c for c in (CsvLog, DewarGauge, FieldBar, Fold, Led, TimeChart,
                                    SIEdit, pg.PlotWidget)}


class _Loader(QUiLoader):
    def __init__(self, base: QtWidgets.QWidget):
        super().__init__(base)
        self.base = base
        self.custom = _custom_widgets()

    def _keep(self, obj: QtCore.QObject, name: str) -> None:
        if not name:
            return
        if callable(getattr(type(self.base), name, None)) \
                or isinstance(getattr(type(self.base), name, None), property):
            raise AttributeError(f"{type(self.base).__name__}: the form's object name {name!r} "
                                 "would replace a method; rename it in Designer")
        if name in vars(self.base):          # set by __init__ before the form loaded
            raise AttributeError(f"{type(self.base).__name__}: the form's object name {name!r} "
                                 "would replace data the code already holds; rename it in "
                                 "Designer")
        setattr(self.base, name, obj)

    def createWidget(self, class_name, parent=None, name=""):  # noqa: N802 (Qt override)
        if parent is None:
            return self.base                     # the form's top level is the instance
        if class_name in self.custom:
            w = self.custom[class_name]()
            w.setParent(parent)
            w.setObjectName(name)
        else:
            w = super().createWidget(class_name, parent, name)
        self._keep(w, name)
        return w

    def createLayout(self, class_name, parent=None, name=""):  # noqa: N802
        lay = super().createLayout(class_name, parent, name)
        self._keep(lay, name)
        return lay

    def createAction(self, parent=None, name=""):  # noqa: N802
        act = super().createAction(parent, name)
        self._keep(act, name)
        return act

    def createActionGroup(self, parent=None, name=""):  # noqa: N802
        group = super().createActionGroup(parent, name)
        self._keep(group, name)
        return group


def load_ui(name: str, base: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """Build the form <name>.ui into `base` (whose class must match the form's
    top-level class: QWidget, QMainWindow or QDialog)."""
    if name not in forms():
        raise FileNotFoundError(f"no form {name}.ui in {FORMS_DIR}")
    path = forms()[name]
    f = QtCore.QFile(str(path))
    if not f.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
        raise FileNotFoundError(path)
    try:
        loader = _Loader(base)
        w = loader.load(f)
    finally:
        f.close()
    if w is None:
        raise RuntimeError(f"{path.name}: {loader.errorString()}")
    return w
