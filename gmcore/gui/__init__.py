"""Instrument control GUI (needs the `gui` extra: PySide6, PySide6-QtAds, pyqtgraph,
tomlkit, QtAwesome)."""

import os

# pyqtgraph picks the first Qt binding it finds; the GUI is PySide6 only (QtAds
# is a PySide6 build), so pin it before anything imports pyqtgraph.
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
os.environ.setdefault("QT_API", "pyside6")     # QtAwesome (icons) goes through QtPy
