"""sweepkit: asyncio sweep engine (see 'Sweep Engine — Project Plan.md').

    import sweepkit
    ds = sweepkit.load("run.h5")                      # xarray.Dataset
    stop = await sweepkit.run_scan(scan, devices, out="run.h5")
"""

__version__ = "0.0.1"


def load(path):
    """Load a run file as an xarray.Dataset on the scan grid (needs xarray)."""
    from .dataset import load as _load
    return _load(path)


def __getattr__(name):          # lazy: importing sweepkit stays cheap
    if name == "run_scan":
        from .api import run_scan
        return run_scan
    raise AttributeError(f"module 'sweepkit' has no attribute {name!r}")
