"""Collect run provenance: software, host, git commit, station config text."""

from __future__ import annotations

import getpass
import platform
import socket
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from . import __version__


def _git_commit(cwd: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                               text=True, timeout=5).stdout.strip()
        return (out or "unknown") + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def collect(station_toml: str | Path | None = None) -> dict:
    pkgs = {}
    for p in ("numpy", "h5py", "pydantic", "pyvisa"):
        try:
            pkgs[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            pass
    info = {"generalmeasurements_version": __version__,
            "git_commit": _git_commit(Path(__file__).parent),
            "python": sys.version.split()[0], "packages": pkgs,
            "host": socket.gethostname(), "platform": platform.platform(),
            "user": getpass.getuser()}
    if station_toml is not None:
        info["station_toml"] = Path(station_toml).read_text("utf-8")
    return info
