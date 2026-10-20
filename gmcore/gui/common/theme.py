"""Bright, dark or follow-system colours: one setting for every window.

Every main window has a Miscellaneous menu with the three choices
(`add_menu`). Picking one anywhere changes the whole application at once and
ticks the same entry in every window's menu; the choice is remembered between
sessions (`[appearance] theme` in `Instrument config/misc.toml`, appsettings.py).

Windows are drawn with the Fusion style (the native Windows 10 style has no dark
colours). Bright is Fusion's own palette, dark is _DARK; "Follow system" picks
between them from the system's colour scheme, and follows it when it changes.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import appsettings, icons

MODES = ("system", "bright", "dark")
LABELS = {"system": "Follow system", "bright": "Bright", "dark": "Dark"}
KEY = "appearance/theme"
_SCHEMES = {"system": QtCore.Qt.ColorScheme.Unknown, "bright": QtCore.Qt.ColorScheme.Light,
            "dark": QtCore.Qt.ColorScheme.Dark}



def settings() -> appsettings.MiscSettings:
    return appsettings.misc()


class _Hub(QtCore.QObject):
    changed = QtCore.Signal(str)


class _Choices(QtGui.QActionGroup):
    """One menu's three entries; a bound slot, so a closed window's menu drops out."""

    def __init__(self, menu: QtWidgets.QMenu):
        super().__init__(menu)
        self.by_mode: dict[str, QtGui.QAction] = {}
        for mode in MODES:
            act = menu.addAction(LABELS[mode])
            act.setObjectName(f"actionTheme_{mode}")
            act.setCheckable(True)
            act.setData(mode)
            self.addAction(act)
            self.by_mode[mode] = act
        self.show_mode(current())
        self.triggered.connect(lambda act: set_mode(act.data()))

    def show_mode(self, mode: str) -> None:
        self.by_mode[mode].setChecked(True)


_hub: _Hub | None = None


def _setup() -> _Hub:
    """Once per application: Fusion style, the saved choice, icon refresh when the
    colours change (also when the system flips while following it)."""
    global _hub
    if _hub is None:
        app = QtWidgets.QApplication.instance()
        _hub = _Hub(app)
        app.setStyle("Fusion")
        QtGui.QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda _scheme: _set_palette())
        _set_scheme(current())
    return _hub


def current() -> str:
    mode = settings().value(KEY, "system")
    return mode if mode in MODES else "system"


_DARK = {"Window": "#2b2b2b", "WindowText": "#f0f0f0", "Base": "#1e1e1e",
         "AlternateBase": "#262626", "ToolTipBase": "#2b2b2b", "ToolTipText": "#f0f0f0",
         "PlaceholderText": "#8c8c8c", "Text": "#f0f0f0", "Button": "#353535",
         "ButtonText": "#f0f0f0", "BrightText": "#ff6b6b", "Light": "#505050",
         "Midlight": "#404040", "Mid": "#5a5a5a", "Dark": "#202020", "Shadow": "#000000",
         "Highlight": "#2e86c1", "HighlightedText": "#ffffff", "Link": "#5dade2",
         "LinkVisited": "#af7ac5"}
_DARK_DISABLED = {"WindowText": "#7f7f7f", "Text": "#7f7f7f", "ButtonText": "#7f7f7f",
                  "Highlight": "#3c3c3c"}


def _dark_palette() -> QtGui.QPalette:
    pal = QtGui.QPalette()
    for role, color in _DARK.items():
        pal.setColor(getattr(QtGui.QPalette.ColorRole, role), QtGui.QColor(color))
    for role, color in _DARK_DISABLED.items():
        pal.setColor(QtGui.QPalette.ColorGroup.Disabled,
                     getattr(QtGui.QPalette.ColorRole, role), QtGui.QColor(color))
    return pal


def is_dark() -> bool:
    """The colours in use now are the dark ones."""
    mode = current()
    if mode == "system":
        return QtGui.QGuiApplication.styleHints().colorScheme() == QtCore.Qt.ColorScheme.Dark
    return mode == "dark"


def _set_scheme(mode: str) -> None:
    # the hint styles the title bars and dialogs the system draws; the palette
    # itself is set too, as not every platform turns the hint into colours
    QtGui.QGuiApplication.styleHints().setColorScheme(_SCHEMES[mode])
    _set_palette()


def _set_palette() -> None:
    app = QtWidgets.QApplication.instance()
    app.setPalette(_dark_palette() if is_dark() else app.style().standardPalette())
    icons.refresh()


def set_mode(mode: str) -> None:
    """Switch every window to `mode` ("system", "bright" or "dark") and remember it."""
    if mode not in MODES:
        raise ValueError(f"theme {mode!r}: one of {', '.join(MODES)}")
    hub = _setup()
    settings().setValue(KEY, mode)
    _set_scheme(mode)
    hub.changed.emit(mode)


def add_menu(window: QtWidgets.QMainWindow) -> QtWidgets.QMenu:
    """The Miscellaneous menu (`window.menuMiscellaneous`) with the theme choices."""
    hub = _setup()
    menu = window.menuBar().addMenu("&Miscellaneous")
    menu.setObjectName("menuMiscellaneous")
    menu.addSection("Theme")
    hub.changed.connect(_Choices(menu).show_mode)
    setattr(window, "menuMiscellaneous", menu)     # any QMainWindow: not a declared attribute
    return menu
