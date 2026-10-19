from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunStart:
    uid: str
    plan: Any
    shape: tuple[int, ...]
    fields: dict
    snapshots: dict
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    seq: int
    index: tuple[int, ...]
    setpoints: dict[str, float]
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Note:
    """Operator-relevant message (retry, pause, watchdog trip): goes to /log and notifiers."""
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RunStop:
    status: str          # success | aborted | failed
    reason: str = ""
