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


def test_general_measurement_lists_the_servers_instruments(app):
    s = InstrumentServer("tests/fixtures/instruments_sim.toml")
    try:
        s.actionGeneralMeasurement.trigger()
        m = s.measurement
        names = [m.instruments.topLevelItem(i).text(0)
                 for i in range(m.instruments.topLevelItemCount())]
        assert names == [r.name for r in s.rows] + ["Timer"]           # the built-in Timer last
        assert m.instruments.topLevelItem(0).text(1) == "not connected"   # none connected
        assert m.log_source.currentData() == "auto"
        assert "empty" in m.estimate.text() and not m.btn_pause.isEnabled()
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


def test_every_icon_role_resolves_and_every_name_is_used(app):
    from gmcore.config.schema import DRIVERS
    from gmcore.gui.common import icons
    for role in icons.ICONS:                                 # a real MDI 6 name
        assert not icons.icon(role).isNull(), role
    roles = set(icons.ICONS)
    assert set(icons.BY_NAME.values()) | set(icons.ACTIONS.values()) | set(
        icons.DRIVERS.values()) | set(icons.COLORS) <= roles
    assert set(DRIVERS) <= set(icons.DRIVERS)                # every driver has a window icon
    names = {w.get("name") for p in uiload.forms().values()
             for w in ET.parse(p).getroot().iter() if w.tag in ("widget", "action")}
    assert set(icons.BY_NAME) <= names, set(icons.BY_NAME) - names   # no typo in a name


def test_forms_get_their_icons(app):
    s = InstrumentServer("tests/fixtures/instruments.toml")
    try:
        assert not s.actionOpen.icon().isNull() and not s.btn_quick_settings.icon().isNull()
        assert all(not s.tabs.tabIcon(i).isNull() for i in range(s.tabs.count()))
        r = s.rows[0]
        assert not r.btn_connect.icon().isNull() and not r.btn_open.icon().isNull()
        w = s.open_panel(r)
        assert not w.windowIcon().isNull() and not w.panel.btn_connect.icon().isNull()
    finally:
        s._dirty = False
        s.close()


def test_theme_menu_is_one_setting_for_every_window(app):
    from PySide6 import QtGui

    from gmcore.gui.common import icons, theme
    s = InstrumentServer("tests/fixtures/instruments.toml")
    try:
        panel_window = s.open_panel(s.rows[0])          # follows the theme, no menu of its own
        assert not hasattr(panel_window, "menuMiscellaneous")
        assert "&Miscellaneous" not in [a.text() for a in panel_window.menuBar().actions()]
        other = QtWidgets.QMainWindow()                  # another main window (legacy station)
        menus = [s.menuMiscellaneous, theme.add_menu(other)]
        assert [a.text() for a in menus[0].actions()
                if a.objectName().startswith("actionTheme_")] == ["Follow system", "Bright",
                                                                  "Dark"]

        def checked(menu):
            return next(a.data() for a in menu.actions()
                        if a.objectName().startswith("actionTheme_") and a.isChecked())

        text = QtGui.QPalette.ColorRole.WindowText
        for mode, dark in (("dark", True), ("bright", False)):
            next(a for a in menus[1].actions() if a.data() == mode).trigger()
            assert [checked(m) for m in menus] == [mode, mode]     # every window agrees
            assert theme.current() == mode                          # remembered
            assert (app.palette().color(QtGui.QPalette.ColorRole.Window).lightness()
                    < 128) == dark
            # icons redrawn in the new text colour
            want = icons.icon("open").pixmap(16).toImage()
            assert s.actionOpen.icon().pixmap(16).toImage() == want
            assert (app.palette().color(text).lightness() > 128) == dark
            app.processEvents()                          # palette changes arrive as events
            assert (panel_window.palette().color(text).lightness() > 128) == dark
        theme.set_mode("system")
        assert [checked(m) for m in menus] == ["system", "system"]
        with pytest.raises(ValueError):
            theme.set_mode("sepia")
    finally:
        s._dirty = False
        s.close()


def test_no_qtawesome_means_no_icons_not_an_error(app, monkeypatch):
    from gmcore.gui.common import icons
    monkeypatch.setattr(icons, "_qta", lambda: None)
    assert icons.icon("connect").isNull()
    w = QtWidgets.QMainWindow()
    uiload.load_ui("panel_window", w)                         # forms still load


def test_form_types_are_up_to_date():
    """form_types.py declares what each form creates; regenerate it after a
    Designer change with `python -m gmcore.gui.common.uiload`."""
    assert uiload.TYPES_PATH.read_text("utf-8") == uiload.declarations(), \
        "gui/common/form_types.py is out of date: run python -m gmcore.gui.common.uiload"
    from gmcore.gui.common import form_types
    for name in FORMS:                               # one declaration class per form
        assert hasattr(form_types, uiload.form_class(name)), name
    common = form_types.PanelCommonForm.__annotations__
    assert {"btn_connect", "btn_safe", "btn_update", "read_time", "interval"} <= set(common)
