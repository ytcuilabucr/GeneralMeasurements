"""General Measurement: the sequence editor (layout preview only).

The window loads forms/general_measurement.ui and lists the Instrument
Server's instruments, so the layout can be judged with real names. Nothing in
it runs yet: every editing and run control is disabled. It shares the server's
process and will use `server.devices` (single owner) once it is built.
"""

from __future__ import annotations

from PySide6 import QtWidgets

from ..common.uiload import load_ui


class GeneralMeasurementWindow(QtWidgets.QMainWindow):
    def __init__(self, server):
        super().__init__()
        self.server = server
        load_ui("general_measurement", self)
        self.actionCloseWindow.triggered.connect(self.close)
        for w in (self.btn_add_sweep, self.btn_add_loop, self.btn_add_read, self.btn_remove,
                  self.btn_up, self.btn_down, self.step_box, self.run_box):
            w.setEnabled(False)
        for a in (self.actionNew, self.actionOpenSequence, self.actionSaveSequence,
                  self.actionSaveSequenceAs, self.actionDryRun, self.actionRun,
                  self.actionPause, self.actionAbort):
            a.setEnabled(False)
        self.statusbar.showMessage("Layout preview: not functional yet")
        self.sequence.expandAll()
        self.refresh_instruments()

    def refresh_instruments(self) -> None:
        """The server's instruments, with their read-out fields once connected."""
        self.instruments.clear()
        for r in self.server.rows:
            item = QtWidgets.QTreeWidgetItem([r.name, r.driver, r.state])
            dev = r.panel.dev if r.panel is not None else None
            if dev is not None:
                for key, info in dev.describe().items():
                    QtWidgets.QTreeWidgetItem(item, [info.label or key, info.units, ""])
            self.instruments.addTopLevelItem(item)
        self.instruments.expandAll()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.refresh_instruments()
        super().showEvent(event)
