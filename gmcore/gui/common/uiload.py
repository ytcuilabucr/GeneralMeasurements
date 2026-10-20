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

For the type checker, form_types.py declares each form's attributes with their
classes (`class ServerForm: btn_connect: QtWidgets.QPushButton ...`); the class
that loads a form also derives from its declarations. form_types.py is
generated from the .ui files: after changing a form in Designer, run

    python -m gmcore.gui.common.uiload

(tests/test_ui.py fails while it is out of date).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from PySide6.QtUiTools import QUiLoader

from . import icons

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
    icons.apply(base)                    # Material Design icons, by object name
    return w


# -- declarations for the type checker (form_types.py) ---------------------------------------
TYPES_PATH = Path(__file__).with_name("form_types.py")
_QTGUI = {"QAction", "QActionGroup"}


def form_class(name: str) -> str:
    """'general_measurement' -> 'GeneralMeasurementForm'."""
    return "".join(part.capitalize() for part in name.split("_")) + "Form"


def _form_block(cls_name: str, doc: str, attrs: dict[str, str], custom: dict[str, str]) -> str:
    lines = [f"class {cls_name}:", f'    """{doc}"""']
    for obj, cls in sorted(attrs.items()):
        if cls in custom:
            qual = f"pyqtgraph.{cls}" if custom[cls] == "pyqtgraph" else cls
        else:
            qual = f"QtGui.{cls}" if cls in _QTGUI else f"QtWidgets.{cls}"
        lines.append(f"    {obj}: {qual}")
    return "\n".join(lines)


def declarations() -> str:
    """form_types.py's source: for each form, the attributes load_ui() sets."""
    import xml.etree.ElementTree as ET

    custom: dict[str, str] = {}                      # class -> module
    body: list[str] = []
    panel_forms: list[dict[str, str]] = []           # forms with a QWidget top level
    for name, path in forms().items():
        root = ET.parse(path).getroot()
        for cw in root.iter("customwidget"):
            custom[cw.findtext("class", "")] = cw.findtext("header", "")
        top = root.find("widget")
        attrs: dict[str, str] = {}
        for el in root.iter():
            obj = el.get("name", "")
            if el is top or not obj:
                continue
            if el.tag in ("widget", "layout"):
                attrs[obj] = el.get("class", "QWidget")
            elif el.tag == "action":
                attrs[obj] = "QAction"
            elif el.tag == "actiongroup":
                attrs[obj] = "QActionGroup"
        if top is not None and top.get("class") == "QWidget":
            panel_forms.append(attrs)
        body.append(_form_block(form_class(name), path.name, attrs, custom))
    # what every panel form has (the shared header and footer): Panel's own attributes
    common = {k: v for k, v in panel_forms[0].items()
              if all(f.get(k) == v for f in panel_forms[1:])} if panel_forms else {}
    body.append(_form_block("PanelCommonForm", "every panel form (QWidget top level)",
                            common, custom))
    imports = ["import pyqtgraph", "from PySide6 import QtGui, QtWidgets"]
    by_module: dict[str, list[str]] = {}
    for cls, module in custom.items():
        if module != "pyqtgraph":
            by_module.setdefault(module, []).append(cls)
    for module, names in sorted(by_module.items()):
        imports.append(f"from {module} import {', '.join(sorted(names))}")
    head = ('"""The widgets, layouts and actions each Designer form creates as attributes\n'
            "(load_ui), for the type checker. Generated from gui/forms/*.ui by\n"
            "`python -m gmcore.gui.common.uiload`: do not edit.\n"
            '"""\n\n'
            "from __future__ import annotations\n\n"
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n" + "".join(f"    {i}\n" for i in imports))
    return head + "\n\n" + "\n\n\n".join(body) + "\n"


if __name__ == "__main__":
    TYPES_PATH.write_text(declarations(), "utf-8")
    print(f"wrote {TYPES_PATH}")
