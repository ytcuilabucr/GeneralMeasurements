"""HDF5 sink: one file per run, laid out on the scan grid (schema in the plan).

Status is "running" from RunStart until RunStop; a killed process therefore
leaves status "running" (honest: the run did not finish). Use `resume_point()`
and `run(..., start_seq=n)` with `HDF5Sink(path, resume=True)` to finish it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import h5py
import numpy as np

from ..engine.events import Event, Note, RunStart, RunStop

SCHEMA_VERSION = 1


def _plan_json(plan) -> str:
    return json.dumps(plan.to_dict(), sort_keys=True, default=str)


def resume_point(path: str | Path) -> int:
    """Number of completed points in an existing run file (= next seq to run)."""
    with h5py.File(path, "r") as f:
        if f.attrs.get("mode") == "fly":
            raise ValueError("fly runs cannot be resumed")
        return int((f["data/seq"][...] >= 0).sum())


class HDF5Sink:
    def __init__(self, path: str | Path, flush_every: int = 10, resume: bool = False):
        self.path, self.flush_every, self.resume = Path(path), flush_every, resume
        self._f: h5py.File | None = None
        self._n = 0

    async def consume(self, sub) -> None:
        """Drain a Subscription (use policy='block': this sink never drops)."""
        try:
            async for item in sub:
                if isinstance(item, RunStart):
                    self._start(item)
                elif isinstance(item, Event):
                    await self._event(item)
                elif isinstance(item, Note):
                    if self._f is not None:
                        self._log(self._f, item.message)
                elif isinstance(item, RunStop):
                    self._stop(item)
        finally:
            if self._f is not None:      # bus closed / cancelled without RunStop
                self._f.attrs["status"] = "interrupted"
                self._f.attrs["t_stop"] = time.time()
                self._f.close()
                self._f = None

    # -- handlers ---------------------------------------------------------
    def _start(self, rs: RunStart) -> None:
        plan_json = _plan_json(rs.plan)
        if self.resume and self.path.exists():
            f = h5py.File(self.path, "r+")
            if f["plan"].attrs["plan_json"] != plan_json:
                f.close()
                raise ValueError("resume: plan differs from the one in the file")
            f.attrs["status"], f.attrs["reason"] = "running", ""
            self._log(f, f"resumed at seq {rs.provenance.get('start_seq', 0)}")
            self._f = f
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = h5py.File(self.path, "w")
        a = f.attrs
        a["schema_version"], a["uid"] = SCHEMA_VERSION, rs.uid
        a["status"], a["reason"] = "running", ""
        a["t_start"], a["t_stop"] = time.time(), 0.0

        p = f.create_group("plan")
        p.attrs["plan_json"] = plan_json
        p.attrs["shape"] = list(rs.shape)
        p.attrs["axis_names"] = list(rs.plan.axis_names)

        sp = f.create_group("setpoints")
        for ax in getattr(rs.plan, "axes", ()):
            sp.create_dataset(ax.device, data=np.asarray(ax.points, dtype=float))

        fly = getattr(rs.plan, "mode", "stepped") == "fly"
        a["mode"] = "fly" if fly else "stepped"
        d = f.create_group("data")
        if fly:   # 1D streams: resizable, one row per sample
            p.attrs["shape"] = []
            sp.create_dataset(rs.plan.device, shape=(0,), maxshape=(None,), dtype="f8")
            for name, info in rs.fields.items():
                ds = d.create_dataset(name, shape=(0,) + tuple(info.shape),
                                      maxshape=(None,) + tuple(info.shape), dtype=info.dtype)
                ds.attrs["units"], ds.attrs["label"] = info.units, info.label or name
            d.create_dataset("timestamp", shape=(0,), maxshape=(None,), dtype="f8")
            d.create_dataset("seq", shape=(0,), maxshape=(None,), dtype="i8")
        for name, info in ({} if fly else rs.fields).items():
            ds = d.create_dataset(name, shape=tuple(rs.shape) + tuple(info.shape),
                                  dtype=info.dtype, fillvalue=np.nan)
            ds.attrs["units"], ds.attrs["label"] = info.units, info.label or name
        if not fly:
            d.create_dataset("timestamp", shape=rs.shape, dtype="f8", fillvalue=np.nan)
            d.create_dataset("seq", shape=rs.shape, dtype="i8", fillvalue=-1)

        s = f.create_group("snapshot")
        for dev, snap in rs.snapshots.items():
            s.attrs[dev] = json.dumps(snap, default=str)

        pv = f.create_group("provenance")
        for k, v in rs.provenance.items():
            pv.attrs[k] = v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)

        f.create_dataset("log/entries", shape=(0,), maxshape=(None,),
                         dtype=h5py.string_dtype())
        self._f = f
        f.flush()

    async def _event(self, ev: Event) -> None:
        f = self._f
        d = f["data"]
        if f.attrs.get("mode") == "fly":
            self._fly_event(f, ev)
        else:
            for name, val in ev.data.items():
                if name not in d:   # field without describe(): scalar, created lazily
                    d.create_dataset(name, shape=f["data/seq"].shape, dtype="f8",
                                     fillvalue=np.nan)
                d[name][ev.index] = val
            d["timestamp"][ev.index] = ev.timestamp
            d["seq"][ev.index] = ev.seq    # written last: marks the point complete
        self._n += 1
        if self._n % self.flush_every == 0:
            await asyncio.to_thread(f.flush)

    def _fly_event(self, f, ev: Event) -> None:
        n = ev.seq + 1
        for dev, v in ev.setpoints.items():
            ds = f[f"setpoints/{dev}"]
            ds.resize(n, axis=0)
            ds[-1] = v
        d = f["data"]
        for name, val in ev.data.items():
            ds = d[name]
            ds.resize(n, axis=0)
            ds[-1] = val
        d["timestamp"].resize(n, axis=0)
        d["timestamp"][-1] = ev.timestamp
        d["seq"].resize(n, axis=0)
        d["seq"][-1] = ev.seq

    def _stop(self, st: RunStop) -> None:
        f = self._f
        f.attrs["status"], f.attrs["reason"] = st.status, st.reason
        f.attrs["t_stop"] = time.time()
        if st.reason:
            self._log(f, f"{st.status}: {st.reason}")
        f.close()
        self._f = None

    @staticmethod
    def _log(f, msg: str) -> None:
        ds = f["log/entries"]
        ds.resize(ds.shape[0] + 1, axis=0)
        ds[-1] = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
