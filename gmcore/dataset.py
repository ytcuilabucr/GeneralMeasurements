"""gmcore.load(path) -> xarray.Dataset on the scan grid."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py

from .sinks.hdf5 import SCHEMA_VERSION, dataset, group


def _attr(obj: h5py.HLObject, key: str) -> Any:
    """An attribute's value (h5py types it as an array; ours are scalars, strings
    and lists)."""
    return obj.attrs[key]


def load(path: str | Path):
    import xarray as xr

    with h5py.File(path, "r") as f:
        ver = int(_attr(f, "schema_version"))
        if ver > SCHEMA_VERSION:
            raise ValueError(f"file schema {ver} is newer than this reader ({SCHEMA_VERSION})")
        plan = group(f, "plan")
        axes = [a if isinstance(a, str) else a.decode() for a in _attr(plan, "axis_names")]
        fly = f.attrs.get("mode", "stepped") == "fly"
        lead = ["sample"] if fly else list(axes)
        coords = {a: ("sample" if fly else a, dataset(f, f"setpoints/{a}")[...]) for a in axes}
        vars_ = {}
        for name, ds in group(f, "data").items():
            if not isinstance(ds, h5py.Dataset):
                raise ValueError(f"{path}: data/{name} is not a dataset")
            dims = lead + [f"{name}_dim{i}" for i in range(ds.ndim - len(lead))]
            out = f"{name}_readback" if name in coords else name
            vars_[out] = (dims, ds[...], {k: v for k, v in ds.attrs.items()})
        attrs = {k: (v.item() if hasattr(v, "item") else v) for k, v in f.attrs.items()}
        attrs["plan"] = json.loads(_attr(plan, "plan_json"))
        attrs["snapshot"] = {k: json.loads(v) for k, v in group(f, "snapshot").attrs.items()}
        attrs["provenance"] = dict(group(f, "provenance").attrs)
        attrs["log"] = [s.decode() if isinstance(s, bytes) else s
                        for s in dataset(f, "log/entries")[...]]
    return xr.Dataset(vars_, coords=coords, attrs=attrs)
