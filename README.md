# sweepkit

A small asyncio sweep engine for the lab: any instrument that implements the
Movable/Readable contract can be swept or read, so a new measurement is a
station config and a plan file, not new code. Design and rationale are in
[`Sweep Engine — Project Plan.md`](Sweep%20Engine%20—%20Project%20Plan.md).

**Status:** the core (plans, runner, fly mode, safety, HDF5, CLI, live plot) is
complete and tested on simulators. **No driver has been run against real
hardware yet** (see [adapter pages](docs/adapters/)).

## Install

```
python -m venv .venv
.venv\Scripts\activate           # Linux: source .venv/bin/activate
pip install -e ".[dev]"          # core + xarray + pytest
pip install -e ".[plot]"         # optional: live plot (pyqtgraph + PyQt6)
pytest                           # ~7 s, no hardware needed
```

Python 3.12+. Real instruments additionally need a VISA backend (NI-VISA, or
`pyvisa-py` which is installed by default for USB/LAN).

## 10-minute quickstart (simulated station, no hardware)

`station.sim.toml` binds role names to simulated devices; `examples/gate_sweep.py`
is a 2D snake scan (bias outer, gate inner) reading a lock-in.

```
sweepkit validate station.sim.toml
sweepkit dry-run examples/gate_sweep.py --station station.sim.toml --read-s 0.2
sweepkit run     examples/gate_sweep.py --station station.sim.toml --out data --sample demo
```

- `validate` checks the TOML with pydantic and touches no hardware; a typo in a
  driver name or field fails here.
- `dry-run` checks every setpoint against the configured limits and prints the
  point count and an estimated duration.
- `run` writes `data/<date>_<time>_<sample>.h5` and serves a control port.

While a run is going, from another terminal:

```
sweepkit ctl status            # state, seq / total, last note
sweepkit ctl pause             # holds at the next point; ramps are held in place
sweepkit ctl resume
sweepkit ctl abort too noisy   # interrupts a move in progress, then safe shutdown
```

Live plot (needs the `plot` extra): `sweepkit run ... --plot lockin.x`
(comma-separated fields; `--plot-x` picks the x axis; `--plot-keep` leaves the
window open). The window is a separate process: it can lag or crash without
touching the measurement.

Read the data back:

```python
import sweepkit
ds = sweepkit.load("data/20261013_223826_demo.h5")   # xarray.Dataset
ds["lockin.x"].plot()                                # dims (bias, gate)
ds.attrs["status"], ds.attrs["provenance"]["git_commit"]
```

Axis readbacks are stored too, as `<axis>_readback`; plot against those when
the true value matters.

## Writing a plan

A plan file defines `SCAN` (and optionally `RETRY`); devices are referred to by
station role name only.

```python
from sweepkit.engine import RetryPolicy, compliance_watchdog, range_watchdog
from sweepkit.plans import Axis, FlyScan, Scan, linear

# stepped, nested, snake: only axes whose setpoint changed are moved
SCAN = Scan(
    axes=(Axis("bias", linear(0, 0.5, 3)),
          Axis("gate", linear(-5, 5, 21), settle_s=0.1, snake=True)),
    readables=("lockin",),
    metadata={"sample": "S1"},
    watchdogs=(compliance_watchdog("gate.compliance"),
               range_watchdog("lockin.x", -1, 1)),
)

# or continuous: one field ramp per gate value, sampled every dt
SCAN = FlyScan("magnet", start=0, stop=1.0, rate=0.05, dt=0.2,
               readables=("lockin",), outer=(Axis("gate", linear(-5, 5, 11)),),
               snake=True, on_sample_error="skip", max_skips=5)

RETRY = RetryPolicy(retries=2, on_exhausted="pause", read_timeout_s=5,
                    device_read_timeouts={"lockin": 20})
```

From a script instead of the CLI:

```python
stop = await sweepkit.run_scan(scan, devices, out="run.h5",
                               shutdown_order=["gate", "bias"], serve_port=8765)
```

## What happens on every run

1. **Validate** before any hardware moves: roles exist, setpoints inside
   device limits, no two devices produce the same field name.
2. **Start**: snapshots of every device go into the file; devices the scan
   uses get `prepare()` (e.g. Keithley output ON at 0).
3. **Points**: checkpoint (pause/abort), move changed axes concurrently,
   settle, read axes (readback) and readables concurrently, emit, run watchdogs.
4. **Failure policy**: transient `TimeoutError`/`DriverError` are retried per
   `RetryPolicy`, then abort or pause for the operator. A watchdog trip aborts.
   A dead HDF5 writer aborts the run (never silently loses data).
5. **Every exit path** (success, abort, error, cancel, Ctrl+C) runs
   `safe_state()` on all devices in `shutdown_order`, each bounded by a
   timeout, then writes an honest final status. A killed process leaves
   `status="running"`; resume it with `HDF5Sink(path, resume=True)` and
   `start_seq=sweepkit.sinks.resume_point(path)` (stepped scans).

## Layout

```
sweepkit/
  devices/   protocols.py, transport.py, sim.py, keithley_24xx.py, keithley_2000.py, srs_lockin.py
  plans/     axes.py, scan.py (stepped), fly.py (FlyScan)
  engine/    runner.py, fly.py, bus.py, control.py, safety.py, retry.py, remote.py, events.py
  sinks/     hdf5.py, liveplot.py, notify.py
  config/    schema.py (pydantic), registry.py
  api.py  cli.py  dataset.py  provenance.py
docs/        adapters/*.md, adding-an-adapter.md
examples/    gate_sweep.py
```

## Conventions

- SI units at the device interface; settling logic lives inside `set()`.
- One process owns each instrument session. Other services talk to the
  control port (`127.0.0.1`, unauthenticated: never expose it to a network).
- A bug found on hardware gets a simulator or golden-transcript test that
  reproduces it before it is fixed.
- Hardware tests only on dummy loads or resistors, never a real sample.

See [`docs/adding-an-adapter.md`](docs/adding-an-adapter.md) and
[`CHANGELOG.md`](CHANGELOG.md).
