"""The program's priority and CPU cores (gmcore/process.py) and the server's
Miscellaneous → Process menu."""

import os
import shutil
import subprocess
import sys

import pytest

from gmcore import process


class Settings(dict):
    def value(self, key, default=None):
        return self.get(key, default)

    def setValue(self, key, value):  # noqa: N802
        self[key] = value


def test_settings_default_to_high_priority_on_all_cores():
    assert process.from_settings(Settings()) == ("high", [])
    every = process.all_cores()
    s = Settings({process.PRIORITY_KEY: "realtime",          # not offered: the default
                  process.CORES_KEY: [every[-1], 9999, "2", True]})
    assert process.from_settings(s) == ("high", [every[-1]])   # other PCs' cores dropped


def test_check_refuses_what_cannot_be_set():
    with pytest.raises(ValueError, match="one of normal"):
        process.check("realtime", [])
    with pytest.raises(ValueError, match="this computer has"):
        process.check("high", [9999])
    assert process.check("normal", [1, 0, 1]) == [0, 1]


@pytest.mark.skipif(not process.can_set_cores(), reason="cores can't be chosen here")
def test_apply_sets_this_process():
    """In a child process, so the test run itself keeps its priority and cores."""
    every = process.all_cores()
    want = every[:2]
    code = ("from gmcore import process as p; "
            f"print(p.apply('above_normal', {want!r})); print(p.priority(), p.cores())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=os.getcwd()).stdout.splitlines()
    if process.can_set_priority():
        assert out[0].startswith("priority Above normal, cores ")
        assert out[1] == f"above_normal {want}"
    else:
        assert out[1].endswith(str(want))


# -- the server's menu -------------------------------------------------------------------
@pytest.fixture
def server(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6QtAds")
    pytest.importorskip("pyqtgraph")
    from PySide6 import QtWidgets
    from gmcore.gui.common.appsettings import MiscSettings
    from gmcore.gui.server import InstrumentServer
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    applied = []
    monkeypatch.setattr(process, "apply",                 # never changes the test run
                        lambda prio, cores: applied.append((prio, list(cores))) or "ok")
    shutil.copy("tests/fixtures/instruments.toml", tmp_path / "lab.toml")
    s = InstrumentServer(tmp_path / "lab.toml", MiscSettings(tmp_path / "misc.toml"))
    s.applied = applied
    yield s
    s._dirty = False
    s.hide()
    s.worker.stop()


def test_menu_shows_and_changes_the_setting(server, tmp_path):
    assert server.priority_actions["high"].isChecked()
    assert server.actionCores.text() == "CPU cores: all…"
    assert server.apply_process() and server.applied == [("high", [])]
    server.priority_actions["above_normal"].trigger()        # applied at once and kept
    every = process.all_cores()
    server.set_process(cores=every[:2])
    assert server.applied[-2:] == [("above_normal", []), ("above_normal", every[:2])]
    assert server.actionCores.text() == f"CPU cores: {', '.join(map(str, every[:2]))}…"
    text = (tmp_path / "misc.toml").read_text("utf-8")
    assert 'priority = "above_normal"' in text and f"cores = {every[:2]}" in text
    assert "process: ok" in server.log_view.toPlainText()


def test_cores_dialog(server):
    every = process.all_cores()
    dlg = server.cores_dialog()
    boxes = dlg.property("boxes")
    assert len(boxes) == len(every) and all(b.isChecked() for b in boxes)
    assert server.dialog_cores(dlg) == []                    # all ticked = no pinning
    for b in boxes[2:]:
        b.setChecked(False)
    assert server.dialog_cores(dlg) == every[:2]
    for b in boxes[:2]:
        b.setChecked(False)
    from PySide6 import QtWidgets
    ok = dlg.findChild(QtWidgets.QDialogButtonBox).button(
        QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()                                # none ticked: can't accept
