"""The app's own settings, kept in one readable file: `Instrument config/misc.toml`.

    [appearance]
    theme = "dark"                       # system / bright / dark

    [instrument_server]
    last_config = "Instrument config/lab.toml"
    open_last = true                     # open last_config at start

    [quick_view]
    hidden = ["Keithley 2400 - 1.i"]     # readings hidden in the Quick view

The GUI writes it whenever a setting changes, keeping comments and layout; it
can also be edited by hand while the app is closed. A missing or unreadable
file means the defaults. The instrument configs themselves stay in their own
files: this one only says which was used last.

`MiscSettings` reads and writes with QSettings' names (`value`, `setValue`,
"section/key"), so code can take either. One instance is shared (`misc()`), so
two windows never overwrite each other's changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

CONFIG_DIR = Path("Instrument config")
MISC_PATH = CONFIG_DIR / "misc.toml"
HEADER = ("GeneralMeasurements: the app's own settings (theme, last instrument config, "
          "...).\nWritten by the GUI; edit only while it is closed.")


class MiscSettings:
    def __init__(self, path: str | Path = MISC_PATH):
        self.path = Path(path)
        self.error = ""                     # why the file couldn't be read, if it couldn't
        try:
            self.doc = tomlkit.parse(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self.doc = self._new()
        except (OSError, TOMLKitError) as e:
            self.error = f"{self.path}: {e}; using defaults"
            self.doc = self._new()

    @staticmethod
    def _new() -> tomlkit.TOMLDocument:
        doc = tomlkit.document()
        for line in HEADER.splitlines():
            doc.add(tomlkit.comment(line))
        return doc

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        section, _, name = key.rpartition("/")
        return section or "general", name

    def value(self, key: str, default: Any = None, type: Any = None) -> Any:  # noqa: A002
        section, name = self._split(key)
        table = self.doc.get(section)
        if not isinstance(table, dict) or name not in table:
            return default
        v = table[name]
        v = v.unwrap() if hasattr(v, "unwrap") else v
        if type is not None and not isinstance(v, type):
            return default
        return v

    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 (QSettings' name)
        section, name = self._split(key)
        if section not in self.doc:
            self.doc[section] = tomlkit.table()
        self.doc[section][name] = list(value) if isinstance(value, tuple) else value
        self.sync()

    def sync(self) -> None:
        """Write the file now (setValue does this itself)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(tomlkit.dumps(self.doc), "utf-8")
        except OSError as e:
            self.error = f"{self.path}: {e}; settings not saved"


_misc: MiscSettings | None = None


def misc() -> MiscSettings:
    """The shared settings file."""
    global _misc
    if _misc is None:
        _misc = MiscSettings()
    return _misc


def use(settings: MiscSettings | None) -> None:
    """Use `settings` instead of the shared file (tests); None goes back to it."""
    global _misc
    _misc = settings


def stored_path(path: Path) -> str:
    """A path as written in the file: relative to the working folder when inside it."""
    try:
        path = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        path = path.resolve()
    return path.as_posix()
