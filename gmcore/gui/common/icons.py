"""Icons: Material Design Icons (MDI 6) through QtAwesome.

Code asks for an icon by its role (`icon("connect")`), never by the MDI name,
so the look is changed in one place (ICONS). Designer forms get theirs by
object name (BY_NAME), applied by `apply()` when a form is loaded, so a button
named `btn_connect` in any form shows the connect icon. Without QtAwesome
installed the GUI simply has no icons.

Browse the names with `qta-browser` (installed with QtAwesome).
"""

from __future__ import annotations

import os
from functools import cache

os.environ.setdefault("QT_API", "pyside6")      # QtAwesome uses QtPy: pin the binding

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

# role -> MDI 6 name
ICONS: dict[str, str] = {
    # connection and session
    "connect": "mdi6.lan-connect", "disconnect": "mdi6.lan-disconnect",
    "read": "mdi6.refresh", "update": "mdi6.sync", "safe": "mdi6.shield-check",
    "open_panel": "mdi6.open-in-new", "add": "mdi6.plus-box",
    "settings": "mdi6.cog", "fields": "mdi6.format-list-checks",
    "station": "mdi6.monitor-dashboard",
    # files and windows
    "new": "mdi6.file-plus-outline", "open": "mdi6.folder-open",
    "save": "mdi6.content-save", "save_as": "mdi6.content-save-edit",
    "exit": "mdi6.exit-to-app", "close": "mdi6.close", "folder": "mdi6.folder-outline",
    "measurement": "mdi6.chart-timeline-variant", "config": "mdi6.table-cog",
    "quick": "mdi6.view-dashboard-outline",
    # settings rows and actions
    "set": "mdi6.check", "ramp": "mdi6.trending-up", "stop": "mdi6.stop",
    "hold": "mdi6.pause", "zero": "mdi6.arrow-down-bold-circle-outline",
    "power_on": "mdi6.power-on", "power_off": "mdi6.power-off",
    "switch": "mdi6.swap-horizontal", "reset": "mdi6.restore",
    "measure": "mdi6.play-circle-outline", "auto_phase": "mdi6.sine-wave",
    "action": "mdi6.play", "zoom": "mdi6.magnify-scan",
    # detail levels
    "essential": "mdi6.eye-outline", "expanded": "mdi6.eye-plus-outline",
    "all": "mdi6.eye-check-outline",
    # sequences (General Measurement)
    "run": "mdi6.play", "pause": "mdi6.pause", "abort": "mdi6.stop-circle",
    "dry_run": "mdi6.clipboard-check-outline", "sweep": "mdi6.chart-line",
    "loop": "mdi6.repeat", "remove": "mdi6.delete-outline", "up": "mdi6.arrow-up",
    "down": "mdi6.arrow-down",
    # instruments (window icons)
    "app": "mdi6.lan", "smu": "mdi6.flash", "dmm": "mdi6.gauge",
    "lockin": "mdi6.sine-wave", "temperature": "mdi6.thermometer",
    "magnet": "mdi6.magnet", "light": "mdi6.spotlight-beam", "laser": "mdi6.laser-pointer", "helium": "mdi6.water-percent", "rf": "mdi6.radio-tower",
    "sim": "mdi6.flask-outline", "instrument": "mdi6.flask-outline",
}
# roles drawn in a colour of their own (others follow the palette)
COLORS = {"stop": "#c0392b", "abort": "#c0392b", "safe": "#1e8449", "power_on": "#c0392b"}

# Designer form object name -> role
BY_NAME = {
    "btn_connect": "connect", "btn_disconnect": "disconnect", "btn_read": "read",
    "btn_safe": "safe", "btn_update": "update", "btn_add": "add",
    "btn_quick_settings": "settings", "btn_connect_all": "connect",
    "btn_safe_all": "safe", "btn_disconnect_all": "disconnect", "actionOpen": "open", "actionSave": "save",
    "actionSaveAs": "save_as", "actionExit": "exit",
    "actionGeneralMeasurement": "measurement", "actionFields": "fields",
    "actionStation": "station", "config_tab": "config", "quick_tab": "quick",
    # cryostat panels
    "btn_ramp": "ramp", "btn_hold": "hold", "btn_zero": "zero",
    "btn_measure": "measure", "btn_set_interval": "set",
    # General Measurement
    "actionNew": "new", "actionOpenSequence": "open", "actionSaveSequence": "save",
    "actionSaveSequenceAs": "save_as", "actionCloseWindow": "close",
    "actionDryRun": "dry_run", "actionRun": "run", "actionPause": "pause",
    "actionAbort": "abort", "btn_dry_run": "dry_run", "btn_run": "run",
    "btn_pause": "pause", "btn_abort": "abort", "btn_browse": "folder",
    "btn_add_step": "sweep", "btn_add_log": "read", "btn_item_add": "add",
    "btn_step_remove": "remove", "btn_step_up": "up", "btn_step_down": "down",
    "btn_log_remove": "remove", "btn_log_up": "up", "btn_log_down": "down",
    "btn_item_remove": "remove",
}
# a definition's action property id -> role (others: "action")
ACTIONS = {"configure": "reset", "hold": "hold", "auto_phase": "auto_phase",
           "measure": "measure"}
# driver -> window icon role
DRIVERS = {"keithley_2400": "smu", "keithley_2401": "smu", "keithley_2410": "smu",
           "keithley_2450": "smu", "keithley_2000": "dmm", "sr830": "lockin",
           "sr850": "lockin", "lakeshore_335": "temperature", "lakeshore_625": "magnet",
           "lm510": "helium", "aotf_octal": "light", "ysl_aotf": "light", "ysl_sc_pro8": "laser", "gigatronics_2420b": "rf", "sim_source": "sim",
           "sim_magnet": "sim", "sim_lockin": "sim"}


@cache
def _qta():
    try:
        import qtawesome
    except ImportError:            # optional: no icons, nothing else changes
        return None
    return qtawesome


def icon(role: str, color: str | None = None) -> QtGui.QIcon:
    """The icon for a role (an empty icon when QtAwesome is missing). Without a
    colour of its own it takes the palette's text colour, so it shows in bright
    and dark themes alike."""
    qta = _qta()
    if qta is None:
        return QtGui.QIcon()
    color = color or COLORS.get(role) or QtGui.QGuiApplication.palette().color(
        QtGui.QPalette.ColorRole.WindowText).name()
    return qta.icon(ICONS[role], color=color)


def driver_role(driver: str) -> str:
    return DRIVERS.get(driver, "instrument")


def for_driver(driver: str) -> QtGui.QIcon:
    return icon(driver_role(driver))


# Every icon set through these helpers is remembered on its widget or action
# (Qt properties), so refresh() can redraw it in the new colours.
_ROLE, _COLOR, _KIND = "gm_icon", "gm_icon_color", "gm_icon_kind"


def _mark(target: QtCore.QObject, role: str, color: str | None, kind: str) -> None:
    target.setProperty(_ROLE, role)
    target.setProperty(_COLOR, color or "")
    target.setProperty(_KIND, kind)


def set_icon(target: QtWidgets.QAbstractButton | QtGui.QAction, role: str,
             color: str | None = None) -> None:
    _mark(target, role, color, "icon")
    target.setIcon(icon(role, color))


def set_window_icon(window: QtWidgets.QWidget, role: str) -> None:
    _mark(window, role, None, "window")
    window.setWindowIcon(icon(role))


def set_tab_icon(tabs: QtWidgets.QTabWidget, page: QtWidgets.QWidget, role: str) -> None:
    _mark(page, role, None, "tab")
    tabs.setTabIcon(tabs.indexOf(page), icon(role))


def refresh() -> None:
    """Redraw every marked icon (after the colour scheme changed)."""
    app = QtWidgets.QApplication.instance()
    if app is None or _qta() is None:
        return
    for top in app.topLevelWidgets():
        for obj in [top, *top.findChildren(QtCore.QObject)]:
            role = obj.property(_ROLE)
            if not role:
                continue
            color, kind = obj.property(_COLOR) or None, obj.property(_KIND)
            if kind == "icon":
                obj.setIcon(icon(role, color))
            elif kind == "window":
                obj.setWindowIcon(icon(role))
            elif kind == "tab":
                tabs = obj.parentWidget().parentWidget()     # page -> stack -> QTabWidget
                if isinstance(tabs, QtWidgets.QTabWidget):
                    tabs.setTabIcon(tabs.indexOf(obj), icon(role))


def apply(base: QtWidgets.QWidget) -> None:
    """Icons for a loaded form's widgets, actions and tab pages, by object name.
    Toolbars keep their text next to the icons."""
    if isinstance(base, QtWidgets.QMainWindow):
        base.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    for name, role in BY_NAME.items():
        obj = getattr(base, name, None)
        if isinstance(obj, (QtWidgets.QAbstractButton, QtGui.QAction)):
            set_icon(obj, role)
    for tabs in base.findChildren(QtWidgets.QTabWidget):
        for i in range(tabs.count()):
            role = BY_NAME.get(tabs.widget(i).objectName())
            if role:
                set_tab_icon(tabs, tabs.widget(i), role)
