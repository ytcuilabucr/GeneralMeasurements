"""sweepkit.load(path) -> xarray.Dataset on the scan grid."""

from __future__ import annotations

import json
from pathlib import Path

import h5py

from .sinks.hdf5 import SCHEMA_VERSION


def load(path: str | Path):
    import xarray as xr

    with h5py.File(path, "r") as f:
        ver = int(f.attrs["schema_version"])
        if ver > SCHEMA_VERSION:
            raise ValueError(f"file schema {ver} is newer than this reader ({SCHEMA_VERSION})")
        axes = [a if isinstance(a, str) else a.decode() for a in f["plan"].attrs["axis_names"]]
        coords = {a: (a, f[f"setpoints/{a}"][...]) for a in axes}
        vars_ = {}
        for name, ds in f["data"].items():
            dims = list(axes) + [f"{name}_dim{i}" for i in range(ds.ndim - len(axes))]
            out = f"{name}_readback" if name in coords else name
            vars_[out] = (dims, ds[...], {k: v for k, v in ds.attrs.items()})
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in f.attrs.items()}
        attrs["plan"] = json.loads(f["plan"].attrs["plan_json"])
        attrs["snapshot"] = {k: json.loads(v) for k, v in f["snapshot"].attrs.items()}
        attrs["provenance"] = dict(f["provenance"].attrs)
        attrs["log"] = [s.decode() if isinstance(s, bytes) else s for s in f["log/entries"][...]]
    return xr.Dataset(vars_, coords=coords, attrs=attrs)
