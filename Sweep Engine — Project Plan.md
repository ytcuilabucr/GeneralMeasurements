# Sweep Engine — Project Plan

Oct 13, 2026 · @J

## Purpose and scope

Build a lab-owned, asyncio-based sweep engine where any instrument that implements a small Movable/Readable contract can be swept or read, so a new measurement is a config file and a plan, not new code.

**In scope**

- Device contract (Readable, Movable, Rampable, Configurable) and adapters for the lab's instruments
- Stepped and continuous (fly) sweeps, nested to N dimensions, with snake ordering
- Event stream with pluggable sinks: HDF5 writer, live plot, notifications
- Pause, resume, abort, watchdogs and safe shutdown
- TOML station config validated with pydantic
- Simulated devices for offline development, dry runs and tests

**Non-goals for v1**

- Adaptive or feedback-driven sweeps (planned hooks only)
- A full GUI; v1 is scripts plus a CLI and a live-plot window
- Hardware-timed buffered acquisition (DAQ cards, lock-in buffers) beyond what fly mode covers
- Replacing QCoDeS or Bluesky for other groups; interoperability is via the HDF5 files

## Requirements

The engine must run any sweep axis (voltage, field, temperature, wavelength) through the same code path, and never leave hardware in an unsafe state.

**Functional**

| ID | Requirement |
| --- | --- |
| F1 | Sweep any Movable over a point list: linear, log, explicit list, snake |
| F2 | Nested sweeps of any depth; only axes whose setpoint changed are moved |
| F3 | Read any number of Readables concurrently per point, scalar or array |
| F4 | Record axis readback (actual value), not just the setpoint |
| F5 | Continuous mode: start a ramp, sample all readables every dt until done |
| F6 | Pause, resume and abort at point boundaries, from code, CLI or a remote hook |
| F7 | Swap an instrument by editing the station config only |
| F8 | Dry run against simulated devices, with a time estimate |
| F9 | Resume an interrupted scan from the last completed point into the same file |

**Non-functional**

| ID | Requirement |
| --- | --- |
| N1 | Safety: limits checked before any move; ramp rates enforced in the device; safe shutdown on every exit path |
| N2 | Data integrity: data flushed at least every N points; a crash loses at most N points; every file carries an honest final status |
| N3 | Provenance: each run stores config, plan, instrument snapshots, software version and git commit |
| N4 | Responsiveness: the event loop never blocks; blocking drivers run in threads |
| N5 | Testability: every layer except real adapters is tested without hardware |
| N6 | Python 3.12+, Windows lab PCs and Linux |

## Architecture

Five layers, each talking only to its neighbours: devices know hardware, plans are pure data, the runner executes, the event bus carries what happened, and sinks decide where data goes.

&#91;embedded content: sweep engine architecture · 10 components\]

Config builds the devices and the plan drives the runner; sinks and watchdogs only ever see events, so adding an output or a safety check never touches the runner.

```
sweepkit/
  devices/   protocols.py, sim.py, <one module per instrument>
  plans/     axes.py (linear/log/list/snake), scan.py
  engine/    runner.py, fly.py, events.py, bus.py, control.py, safety.py
  sinks/     hdf5.py, liveplot.py, notify.py, log.py
  config/    schema.py (pydantic), loader.py, registry.py
  cli.py
```

### Device contract

```python
class Readable(Protocol):
    name: str
    def describe(self) -> dict[str, FieldInfo]: ...   # keys, units, shape, dtype
    async def read(self) -> dict[str, float | np.ndarray]: ...

class Movable(Readable, Protocol):
    limits: tuple[float, float]
    async def set(self, value: float) -> None: ...    # returns only when settled

class Rampable(Movable, Protocol):
    async def start_ramp(self, target: float, rate: float) -> None: ...
    async def ramp_done(self) -> bool: ...

class Configurable(Protocol):
    async def snapshot(self) -> dict: ...
    async def safe_state(self) -> None: ...
```

Rules: SI units at the interface; settling logic lives inside `set()`; each adapter owns one driver session and one `asyncio.Lock`; blocking libraries are wrapped with `asyncio.to_thread`.

### Plans

`Axis(device, points, settle_s, snake)` and `Scan(axes, readables, metadata, watchdogs)` are frozen dataclasses. Limits are validated at construction, before any hardware moves. A point iterator yields `(index, setpoints)`; `Scan.shape` drives file layout and time estimates.

### Runner

Stepped loop: checkpoint, move changed axes, settle, `gather` all reads (including axis readbacks), emit an `Event`. Fly loop: `start_ramp`, sample every dt until `ramp_done`, emit the same `Event` type. Both run `safe_shutdown` under `asyncio.shield` in `finally` and always emit `RunStop` with status success, aborted or failed.

### Events and sinks

`RunStart` (uid, plan, shape, field descriptions, snapshots, provenance), `Event` (seq, index, setpoints, data, timestamp) and `RunStop` (status, reason) go to a bus. Each sink subscribes with its own bounded queue; a slow sink drops or backs up without stalling the runner (policy set per sink: the HDF5 writer never drops).

### Control and safety

`Controller` exposes pause, resume and abort; the runner checks it at each point. Watchdogs are `async (event) -> str | None` checks (compliance, temperature, leakage); a non-None result aborts with that reason. Shutdown calls each device's `safe_state()` in a configured order.

## Data format and metadata

One HDF5 file per run, laid out on the scan grid so it opens directly in Python (h5py, xarray) and in Igor Pro's HDF5 browser.

```
<date>_<sample>_<run-uid>.h5
  attrs: schema_version, uid, status, reason, t_start, t_stop
  /plan         attrs: plan as JSON, shape, axis names
  /data/<field> dataset shape = scan.shape + field_shape, units + label attrs, NaN-filled until written
  /data/timestamp, /data/seq
  /setpoints/<axis>  1D point lists
  /snapshot     attrs per device: settings at run start
  /provenance   station TOML text, sweepkit version, git commit, Python and package versions, host, user
  /log          watchdog trips, pauses, retries, operator notes
```

- Fly-mode runs store data as 1D streams (`/data/<field>` with shape `(n,) + field_shape`), since the point count isn't known in advance.
- `schema_version` is bumped on any layout change; readers check it.
- A small `sweepkit.load(path)` returns an `xarray.Dataset` with axes as coordinates and units as attributes.
- Naming convention for files and samples is decided once and enforced by the writer (see open questions).

## Configuration and instrument inventory

A station TOML binds role names to drivers; pydantic validates it at startup with a discriminated union on `driver`, so a typo fails before any hardware is touched.

```toml
[station]
name = "cryostat-A"
shutdown_order = ["gate", "bias", "magnet"]

[devices.gate]
driver = "k24xx_source"
resource = "GPIB0::24::INSTR"
compliance_a = 1e-9
limits = [-10.0, 10.0]
max_rate_v_per_s = 0.5

[devices.vxx]
driver = "agilent344xx_voltmeter"
resource = "USB0::0x2A8D::0x1301::MY123::INSTR"
nplc = 10
```

Plans live in separate TOML or Python files and refer to devices by role name only.

**Instrument inventory (to fill in)**

| Role | Instrument | Interface | Contract | Python library | Adapter status |
| --- | --- | --- | --- | --- | --- |
| Voltage source | Keithley 2400 | GPIB | Movable | pyvisa | Not started |
| Voltmeter | Keithley 2410 | GPIB | Readable | pyvisa | Not started |
| Voltmeter | Agilent DMM (model?) | USB/LAN | Readable | pyvisa | Not started |
| Magnet supply | ? | ? | Rampable | ? | Not started |
| Temperature controller | ? | ? | Rampable | ? | Not started |
| Lock-in | ? | ? | Readable | ? | Not started |
| Monochromator / laser | ? | ? | Movable | ? | Not started |

## Development phases

Build the whole pipeline on simulated hardware first (phases 0 to 2), then add real instruments one at a time; each phase ends with a check you can run.

| Phase | Deliverables | Done when |
| --- | --- | --- |
| 0. Foundations | Repo, uv project, ruff, pyright, pytest, CI; protocols module; config schema skeleton | CI green on an empty test suite; a sample station TOML validates |
| 1. Core on simulators | Sim devices (magnet with real ramp time, DMM returning f(setpoints), noisy lock-in); axes and point iterator; Scan validation; stepped runner; bus; controller | A 2D snake scan on sims runs end to end; abort mid-scan triggers safe\_state; out-of-limit plans are rejected before any move |
| 2. Data and provenance | HDF5 sink, provenance capture, `sweepkit.load()`, resume from last point | Killing the process mid-run leaves a readable file with status; resume finishes it; the file opens in Igor and xarray |
| 3. First real instruments | Keithley 24xx source and voltmeter adapters, Agilent DMM adapter, per-adapter hardware tests | I-V on a known resistor matches Ohm's law within instrument spec; swapping the voltmeter needs only a TOML edit |
| 4. Safety and control | Ramp-rate enforcement, watchdogs, shutdown order, CLI pause/resume/abort, notifier sink | Compliance trip aborts the run and ramps gates to zero; pause holds state for 30 min and resumes cleanly |
| 5. Slow axes and fly mode | Magnet and temperature controller adapters (Rampable), fly runner, readback recording | A field fly sweep and a stepped temperature series both produce correct files |
| 6. Optical axis and arrays | Monochromator/laser adapter, array Readable (spectrometer or camera) | A wavelength sweep stores a spectrum per point with correct shape |
| 7. Usability | Live plot, dry run with time estimate, user guide, example plans, remote control hook | A labmate runs a new measurement from the guide without help |
| 8. Later | Adaptive sweeps, hardware-timed buffered acquisition, run browser | Scoped separately |

## Testing strategy

Everything above the adapters is tested offline on every commit; hardware tests run only when explicitly enabled.

| Level | What | How | When |
| --- | --- | --- | --- |
| Unit | Point iterators, snake order, limit validation, config schema, frame encoding | pytest, pure functions | Every commit |
| Engine | Runner, controller, watchdogs, shutdown order, bus backpressure | pytest-asyncio against sim devices; fault-injecting sims (timeouts, garbage replies, slow reads) | Every commit |
| Data | HDF5 layout, schema version, resume, crash recovery | Write, kill, reopen, compare with `load()` | Every commit |
| Adapter | Command strings and parsing per instrument | Golden SCPI transcripts; pyvisa-sim or a fake TCP server | Every commit |
| Hardware | Real instrument round trips, safe-state behaviour | `SWEEP_LIVE=1`, dummy load or resistor, never a real sample | Before each release and after driver changes |
| Acceptance | Phase "done when" checks | Scripted runs on the rig | End of each phase |

Rule: a bug found on hardware gets a simulator or golden-transcript test that reproduces it before it is fixed.

## Tooling and conventions

One package, one lockfile, and conventions written down before the second contributor arrives.

- **Environment:** Python 3.12+, `uv` for dependencies and the lockfile; `pyproject.toml` only.
- **Core dependencies:** numpy, h5py, pydantic, pyvisa (+ pyvisa-py), httpx; optional extras for xarray, pyqtgraph, pymodbus.
- **Quality:** ruff (lint + format), pyright in strict mode for `devices/`, `engine/` and `plans/`; pre-commit hooks.
- **Git:** main is always runnable; feature branches; tag a release before each cooldown so the data's git commit points at a stable version.
- **Logging:** stdlib `logging` with one logger per device; logs also go into the run file's `/log` group.
- **Instrument ownership:** exactly one process owns each instrument session. Other services (helium monitoring, remote control) talk to that process, never to the instrument directly.
- **Docs:** README with a 10-minute quickstart on simulators; one page per adapter (commands used, quirks, safe state); a CHANGELOG.
- **Adding an adapter:** checklist from the tutorial (protocol layer, client with lock and timeouts, typed results, driver errors, opt-in writes, tests), plus `describe()`, `snapshot()` and `safe_state()`.

## Risks and mitigations

The costliest failures are a damaged sample and silently wrong data, so most mitigations target those two.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Gate or bias jumps on a fragile sample | Destroyed device | Ramp rates enforced inside `set()`; limits checked at plan build; hardware tests only on dummy loads |
| Abort or crash leaves outputs on | Sample damage, magnet quench risk | `safe_state()` under `asyncio.shield`; shutdown order in config; tested by killing runs on sims |
| Setpoint recorded instead of real value | Wrong field or temperature axis | Readback recorded for every Movable; plots use readback by default |
| Instrument timeout mid-scan | Lost run | Retry policy per adapter (reconnect, retry N times), then pause and notify rather than abort |
| Blocking driver freezes the loop | Watchdogs and UI stall | All blocking calls via `to_thread`; a loop-lag monitor logs stalls over 100 ms |
| Two processes talk to one instrument | Interleaved commands, wrong readings | Single-owner rule; other services go through the owner |
| Scope creep toward a general framework | Never finished | Non-goals list; phase gates; anything new goes to phase 8 |
| Only one person understands the code | Lab can't maintain it | Quickstart, adapter pages, example plans; a labmate review at phase 7 |

## Open questions and decisions

**Open questions**

- [ ] Which instruments are in phase 3 and 5 (fill the inventory table)?
- [ ] File and sample naming convention, and where run files are stored and backed up
- [ ] Is analysis mainly in Python, Igor Pro, or both? This sets how much the loader must support.
- [ ] Which lab PCs run the engine (Windows versions, NI-VISA installed)?
- [ ] Does the remote control hook go through the planned Slack bot or a separate interface?
- [ ] Retry policy defaults: retries per point, and pause versus abort after they're used up

**Decision log**

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-10-13 | Build our own engine with a Movable/Readable contract instead of QCoDeS or Bluesky | Existing asyncio drivers; full control of concurrency and safety; lab-scale scope |
| 2026-10-13 | TOML + pydantic for station config | Stdlib reader, comments allowed, validation before hardware is touched |
| 2026-10-13 | HDF5, one file per run, laid out on the scan grid | Opens in Python and Igor; arrays and metadata in one file |
