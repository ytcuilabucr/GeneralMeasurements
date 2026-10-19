"""Qt Designer forms (gui/forms/*.ui): they load, the code finds its widgets, and
Designer-set properties of our own widgets reach them."""

import os
import xml.etree.ElementTree as ET

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6QtAds")
pytest.importorskip("pyqtgraph")
pytest.importorskip("tomlkit")
from PySide6 import QtWidgets  # noqa: E402

from gmcore.gui.common import uiload  # noqa: E402
from gmcore.gui.common.dedicated_panels import CRYO_PANELS  # noqa: E402
from gmcore.gui.common.panels import Panel  # noqa: E402
from gmcore.gui.server import InstrumentServer  # noqa: E402

FORMS = sorted(uiload.forms())
BASES = {"QWidget": QtWidgets.QWidget, "QMainWindow": QtWidgets.QMainWindow,
         "QDialog": QtWidgets.QDialog}


@pytest.fixture
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _top_class(name: str) -> str:
    return ET.parse(uiload.forms()[name]).getroot().find("widget").get("class")


def test_the_forms_are_the_ones_the_code_loads():
    used = {"panel", "lakeshore_335", "lakeshore_625", "lm510", "server", "panel_window",
            "general_measurement", "view_window", "field_picker"}
    assert set(FORMS) == used
    assert all(p.parent == uiload.FORMS_DIR for p in uiload.forms().values())
    assert {c.UI for c in CRYO_PANELS.values()} | {Panel.UI} <= used


@pytest.mark.parametrize("name", FORMS)
def test_every_form_loads(app, name):
    base = BASES[_top_class(name)]()
    assert uiload.load_ui(name, base) is base
    root = ET.parse(uiload.forms()[name]).getroot()
    custom = {c.findtext("class") for c in root.iter("customwidget")}
    assert custom <= set(uiload._custom_widgets())          # every promoted class is known
    for w in root.iter("widget"):                           # every named widget is reachable
        if w.get("name") != root.find("widget").get("name"):
            assert hasattr(base, w.get("name")), w.get("name")
    base.deleteLater()


def test_a_name_that_shadows_a_method_is_refused(app, tmp_path, monkeypatch):
    (tmp_path / "bad.ui").write_text(
        '<ui version="4.0"><class>F</class><widget class="QWidget" name="F">'
        '<layout class="QVBoxLayout" name="lay"><item><widget class="QPushButton" '
        'name="close"/></item></layout></widget></ui>', "utf-8")
    monkeypatch.setattr(uiload, "forms", lambda: {"bad": tmp_path / "bad.ui"})
    with pytest.raises(Exception, match="would replace a method"):
        uiload.load_ui("bad", QtWidgets.QWidget())


def test_designer_properties_reach_our_widgets(app):
    from gmcore.config import load_station
    cfg = load_station("tests/fixtures/instruments.toml").devices["Lake Shore 335 - 1"]

    class Host:
        def log(self, *a):
            pass
    p = CRYO_PANELS["lakeshore_335"]("t", cfg, Host())
    assert set(p.chart.curves) == {"A", "B", "setpoint"}             # curves_spec
    assert p.chart.yLabel == "temperature" and p.chart.yUnit == "K"
    assert p.stable_led["A"]._text == "stable"                      # Led label
    assert p.sp.unit == "K" and p.rate.unit == "K/min"              # SIEdit unit
    assert p.tol.value() == pytest.approx(cfg.tolerance)            # code sets from config
    assert p.card["A"].caption.text() == "<b>Temp A</b>"
    assert p.title.property("panelTitle") is True
    assert p.sp_box.isAncestorOf(p.btn_update)                     # Update settings
    p.deleteLater()


def test_general_measurement_preview_lists_the_servers_instruments(app):
    s = InstrumentServer("tests/fixtures/instruments_sim.toml")
    try:
        s.actionGeneralMeasurement.trigger()
        m = s.measurement
        names = [m.instruments.topLevelItem(i).text(0)
                 for i in range(m.instruments.topLevelItemCount())]
        assert names == [r.name for r in s.rows]
        assert not m.run_box.isEnabled() and not m.actionRun.isEnabled()  # preview only
    finally:
        s._dirty = False
        s.close()


def test_a_name_that_shadows_existing_data_is_refused(app, tmp_path, monkeypatch):
    (tmp_path / "bad.ui").write_text(
        '<ui version="4.0"><class>F</class><widget class="QWidget" name="F">'
        '<layout class="QVBoxLayout" name="lay"><item><widget class="QWidget" '
        'name="charts"/></item></layout></widget></ui>', "utf-8")
    monkeypatch.setattr(uiload, "forms", lambda: {"bad": tmp_path / "bad.ui"})
    w = QtWidgets.QWidget()
    w.charts = []                                       # as Panel.__init__ does
    with pytest.raises(Exception, match="replace data"):
        uiload.load_ui("bad", w)
