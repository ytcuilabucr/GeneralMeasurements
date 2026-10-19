"""Live plot sink.

Design: the GUI runs in its own process (Qt wants a main thread and must never
stall the asyncio loop, N4). The sink only turns bus items into small picklable
dict messages and puts them on a bounded queue with put_nowait: a slow or dead
window drops plot updates and never affects the run.

`PlotBuffer` is the pure data model (testable without Qt); `_window_main` is
the thin pyqtgraph view. Scalar fields only; array readables are ignored.
"""

from __future__ import annotations

import logging
import queue
from typing import Any

import numpy as np

from ..engine.events import Event, Note, RunStart, RunStop

log = logging.getLogger("gmcore.liveplot")


# -- data model --------------------------------------------------------------
class PlotBuffer:
    """Accumulates events; answers 'what should a 1D curve / 2D image show now'."""

    def __init__(self):
        self.start({"axis_names": [], "shape": [], "mode": "stepped",
                    "axis_points": {}, "fields": {}})

    def start(self, meta: dict) -> None:
        self.axis_names: list[str] = list(meta["axis_names"])
        self.mode: str = meta["mode"]
        self.shape: tuple[int, ...] = tuple(meta["shape"])
        self.axis_points: dict[str, np.ndarray] = {
            k: np.asarray(v, dtype=float) for k, v in meta["axis_points"].items()}
        self.units: dict[str, str] = dict(meta["fields"])
        self.n = 0
        self.last_index: tuple[int, ...] = ()
        if self.mode == "stepped":
            self._grid = {f: np.full(self.shape, np.nan) for f in self.units}
            self._sp = {a: np.full(self.shape, np.nan) for a in self.axis_names}
        else:                                  # fly: append-only streams
            self._stream: dict[str, list[float]] = {f: [] for f in self.units}
            self._sp_stream: dict[str, list[float]] = {a: [] for a in self.axis_names}

    def add(self, index: tuple[int, ...], setpoints: dict, data: dict) -> None:
        self.n += 1
        if self.mode == "stepped":
            self.last_index = tuple(index)
            for f, v in data.items():
                if f in self._grid and np.ndim(v) == 0:
                    self._grid[f][index] = float(v)
            for a, v in setpoints.items():
                if a in self._sp:
                    self._sp[a][index] = float(v)
        else:
            self.last_index = tuple(index)
            for f, v in data.items():
                if f in self._stream and np.ndim(v) == 0:
                    self._stream[f].append(float(v))
            for a, v in setpoints.items():
                if a in self._sp_stream:
                    self._sp_stream[a].append(float(v))
            n = max((len(s) for s in self._stream.values()), default=0)
            for s in self._stream.values():     # keep columns equal length
                s.extend([np.nan] * (n - len(s)))
            n = self.n
            for s in self._sp_stream.values():
                s.extend([np.nan] * (n - len(s)))

    def series(self, y: str, x: str | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Curve of field y. Stepped: along the innermost axis at the current outer
        index (the row being measured). Fly: the whole stream. `x` may name an axis
        or any data field (e.g. the measured readback); default is the innermost axis."""
        if self.mode == "fly":
            ys = np.asarray(self._stream[y])
            xname = x or self.axis_names[-1]
            xs = (np.asarray(self._sp_stream[xname]) if xname in self._sp_stream
                  else np.asarray(self._stream[xname]))
            return xs, ys
        row = self.last_index[:-1] + (slice(None),) if self.last_index else (slice(None),)
        ys = self._grid[y][row]
        xname = x or self.axis_names[-1]
        if xname in self._grid:
            xs = self._grid[xname][row]
        else:
            xs = self.axis_points[xname]
        return np.asarray(xs), np.asarray(ys)

    def image(self, y: str) -> np.ndarray | None:
        """2D slice over the last two axes at the current outer index (stepped, >=2 axes)."""
        if self.mode != "stepped" or len(self.shape) < 2:
            return None
        outer = self.last_index[:-2] if self.last_index else ()
        return np.array(self._grid[y][outer + (slice(None), slice(None))])


# -- sink ----------------------------------------------------------------------
class _ProcessChannel:
    """Spawns the plot window process; put_nowait never blocks."""

    def __init__(self, fields, x, title, keep_open):
        import multiprocessing as mp
        _require_pyqtgraph()
        ctx = mp.get_context("spawn")
        self.q = ctx.Queue(maxsize=2000)
        self.keep_open = keep_open
        self.proc = ctx.Process(target=_window_main, args=(self.q, fields, x, title),
                                daemon=not keep_open)
        self.proc.start()

    def put_nowait(self, msg: dict) -> None:
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            pass

    def close(self) -> None:
        if self.keep_open:
            return                      # window stays until the user closes it
        self.put_nowait({"kind": "quit"})
        self.proc.join(timeout=2)
        if self.proc.is_alive():
            self.proc.terminate()

    def wait(self) -> None:
        self.proc.join()


def _require_pyqtgraph() -> None:
    try:
        import pyqtgraph  # noqa: F401
    except ImportError as e:
        raise ImportError("live plotting needs pyqtgraph and a Qt binding: "
                          "pip install pyqtgraph PyQt6") from e


class LivePlotSink:
    """fields: scalar fields to plot (default: first 4 non-axis fields).
    x: x-axis override (axis name or data field). channel: injectable for tests."""

    def __init__(self, fields: list[str] | None = None, x: str | None = None,
                 title: str = "gmcore", keep_open: bool = False, channel: Any = None):
        self.fields, self.x, self.title, self.keep_open = fields, x, title, keep_open
        self._channel = channel

    def _chan(self):
        if self._channel is None:
            self._channel = _ProcessChannel(self.fields, self.x, self.title, self.keep_open)
        return self._channel

    @staticmethod
    def _meta(rs: RunStart) -> dict:
        plan = rs.plan
        pts: dict[str, list[float]] = {}
        for ax in getattr(plan, "axes", ()):
            pts[ax.device] = list(ax.points)
        for ax in getattr(plan, "outer", ()):
            pts[ax.device] = list(ax.points)
        return {"kind": "start", "mode": getattr(plan, "mode", "stepped"),
                "axis_names": list(plan.axis_names), "shape": list(rs.shape),
                "axis_points": pts,
                "fields": {k: v.units for k, v in rs.fields.items() if not v.shape}}

    async def consume(self, sub) -> None:
        ch = self._chan()
        try:
            async for item in sub:
                if isinstance(item, RunStart):
                    ch.put_nowait(self._meta(item))
                elif isinstance(item, Event):
                    ch.put_nowait({"kind": "event", "index": tuple(item.index),
                                   "setpoints": dict(item.setpoints),
                                   "data": {k: float(v) for k, v in item.data.items()
                                            if np.ndim(v) == 0}})
                elif isinstance(item, Note):
                    ch.put_nowait({"kind": "note", "text": item.message})
                elif isinstance(item, RunStop):
                    ch.put_nowait({"kind": "stop", "status": item.status,
                                   "reason": item.reason})
        finally:
            ch.close()


# -- view (separate process) ---------------------------------------------------
def _window_main(q, fields, x, title) -> None:  # pragma: no cover - needs a display
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore

    app = pg.mkQApp(title)
    win = pg.GraphicsLayoutWidget(title=title, show=True)
    buf = PlotBuffer()
    views: dict[str, dict] = {}
    state = {"dirty": False, "quit": False}

    def build(meta: dict) -> None:
        win.clear()
        views.clear()
        axes = set(meta["axis_names"])
        names = fields or [f for f in meta["fields"] if f not in axes][:4]
        for f in names:
            if f not in meta["fields"]:
                continue
            p = win.addPlot(title=f"{f} [{meta['fields'][f]}]")
            p.showGrid(x=True, y=True)
            view = {"curve": p.plot(pen=pg.mkPen(width=2), symbol="o", symbolSize=4)}
            if meta["mode"] == "stepped" and len(meta["shape"]) >= 2:
                img = pg.ImageItem()
                ip = win.addPlot(title=f"{f} (map)")
                ip.addItem(img)
                view["image"] = img
            views[f] = view
            win.nextRow()

    def redraw() -> None:
        for f, v in views.items():
            xs, ys = buf.series(f, x)
            ok = np.isfinite(ys) & np.isfinite(xs)
            v["curve"].setData(xs[ok], ys[ok])
            if "image" in v:
                img = buf.image(f)
                if img is not None and np.isfinite(img).any():
                    v["image"].setImage(np.nan_to_num(img, nan=float(np.nanmin(img))).T)

    def poll() -> None:
        for _ in range(500):
            try:
                m = q.get_nowait()
            except queue.Empty:
                break
            k = m["kind"]
            if k == "start":
                buf.start(m)
                build(m)
            elif k == "event":
                buf.add(m["index"], m["setpoints"], m["data"])
                state["dirty"] = True
            elif k == "stop":
                win.setWindowTitle(f"{title} - {m['status']}"
                                   + (f": {m['reason']}" if m["reason"] else ""))
            elif k == "quit":
                app.quit()
                return
        if state["dirty"]:
            redraw()
            state["dirty"] = False

    timer = QtCore.QTimer()
    timer.timeout.connect(poll)
    timer.start(100)
    app.exec()
