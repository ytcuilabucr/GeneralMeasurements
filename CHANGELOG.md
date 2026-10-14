# Changelog

## 0.0.1 (unreleased)

Everything below was built and tested on simulators and fake transports. No
real instrument has been connected yet.

### Devices
- Contracts: Readable, Movable, Rampable, Configurable (`devices/protocols.py`).
- Keithley 2400 / 2401 / 2410 / 2450 SMU, Keithley 2000 DMM, SRS SR830 /
  SR850 lock-in adapters; async transport with per-session lock, cancel-safe.
- Simulators: source, lock-in, magnet (real ramp time), `FaultyDevice` for
  fault injection (timeout, error, delay, garbage).

### Plans and engine
- Stepped `Scan` (N-D, snake, only changed axes move) and `FlyScan`
  (continuous ramp, optional stepped outer axes, snake, sample-miss policy).
- Runner: validate before any move, concurrent reads, readback recording,
  pause / resume / abort (abort interrupts in-flight moves), retry policy
  (per-point and per-device read timeouts, abort or pause when exhausted),
  shielded and time-bounded safe shutdown in configured order.
- Event bus with supervised sinks: a failed critical sink aborts the run.
- Watchdogs: compliance, range, NaN; event-loop lag monitor.
- Control server (localhost TCP) and CLI: `validate`, `dry-run`, `run`, `ctl`.

### Data
- HDF5 sink (one file per run on the scan grid, fly runs as streams),
  provenance capture, `sweepkit.load()` to xarray, resume of interrupted
  stepped runs, schema version 1.
- Notifier sink (webhook) and live-plot sink (pyqtgraph window process).

### Known gaps
- No driver verified on hardware; SCPI strings need golden transcripts.
- Keithley 2450 `read()` likely wrong (single-value reply).
- No lock-in overload detection.
- Phase 6 optical axis / array Readable and phase 8 items not started.
