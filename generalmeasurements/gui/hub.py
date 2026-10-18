"""DataHub: every panel's readings, re-broadcast by field key.

Panels are the only thing that polls drivers. Each poll's data reaches the hub
through `Panel.reading`; monitor and chart windows subscribe to the hub, never
to panels or drivers, so opening more windows never adds instrument traffic.

Field keys are the devices' describe() keys (`magnet`, `magnet.v`, `temp`,
`temp.setpoint`, ...); extras a panel adds to its poll data (`_ramping`, ...)
are not published. The latest value per field is kept for windows opened
later; a device that disconnects drops its latest values.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore


@dataclass
class Field:
    key: str
    device: str
    label: str
    units: str


class DataHub(QtCore.QObject):
    updated = QtCore.Signal(str, float, float)      # field key, wall-clock time, value
    device_changed = QtCore.Signal(str, bool)       # device name, connected

    def __init__(self):
        super().__init__()
        self._fields: dict[str, Field] = {}         # every field seen, kept after disconnect
        self._latest: dict[str, tuple[float, float]] = {}
        self._up: set[str] = set()

    def attach(self, panel) -> None:
        """Follow one panel: its readings and its connection state."""
        panel.reading.connect(self.publish)
        panel.connected.connect(lambda name, p=panel: self.device_up(name, p.dev.describe()))
        panel.status_changed.connect(self._status)

    def _status(self, name: str, state: str) -> None:
        if state == "disconnected":
            self.device_down(name)

    def device_up(self, name: str, describe: dict) -> None:
        for key, info in describe.items():
            self._fields[key] = Field(key, name, info.label or key, info.units)
        self._up.add(name)
        self.device_changed.emit(name, True)

    def device_down(self, name: str) -> None:
        if name not in self._up:
            return
        self._up.discard(name)
        for key, f in self._fields.items():
            if f.device == name:
                self._latest.pop(key, None)
        self.device_changed.emit(name, False)

    def publish(self, name: str, t: float, data: dict) -> None:
        for key, value in data.items():
            f = self._fields.get(key)
            if f is None or f.device != name:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue                             # arrays etc.: not for big numbers
            self._latest[key] = (t, v)
            self.updated.emit(key, t, v)

    # -- queries ---------------------------------------------------------------------
    def fields(self) -> list[Field]:
        """Fields of the connected devices, in connection order."""
        return [f for f in self._fields.values() if f.device in self._up]

    def field(self, key: str) -> Field | None:
        return self._fields.get(key)

    def latest(self, key: str) -> tuple[float, float] | None:
        return self._latest.get(key)

    def is_up(self, device: str) -> bool:
        return device in self._up
