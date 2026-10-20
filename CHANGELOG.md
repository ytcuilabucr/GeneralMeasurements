# Changelog

## 0.0.1 (unreleased)

Built and tested on simulators and fake transports. On real hardware so far:
communication with the Keithley 2400, 2401 and 2000 (outputs off), and
read-only queries to the Lake Shore 335, 625, Giga-tronics 2420B and SR850.

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
  provenance capture, `gmcore.load()` to xarray, resume of interrupted
  stepped runs, schema version 1.
- Notifier sink (webhook) and live-plot sink (pyqtgraph window process).

### Added
- **YSL Photonics** instruments, from the vendor's Python drivers, on a COM port
  through VISA (`SerialBytesTransport`: raw bytes, reply ends when the port
  goes quiet; `resource = "COM<n>"`). Not yet run on the hardware.
  - **SC-PRO8 laser** (`ysl_sc_pro8`, `docs/adapters/ysl_sc_pro8.md`): emission
    (confirmed; only after a power was set this session), power %, repetition
    rate; state and alarm queried; safe state = emission off.
  - **YSL AOTF** (`ysl_aotf`, `docs/adapters/ysl_aotf.md`): one line's
    wavelength and power, binary CRC frames identical to the vendor's;
    control only (no query at all); usable range 273-1424 nm from the vendor's
    cubic; safe state = the vendor's turn-off.
- **AOTF** (`aotf_octal`): the Fianium supercontinuum's Crystal Technology octal
  AOTF controller, one channel's wavelength (nm) and power (%), from the
  vendor's command reference and the lab's LabVIEW program
  (`docs/adapters/aotf_octal.md`).
  - USB through the vendor's 32-bit `AotfLibrary.dll`, run in msl-loadlib's
    32-bit server (`aotf_server32.py`, `AotfLibraryTransport`; extra `aotf`).
  - Wavelength → RF frequency from the calibration file's cubic; power as % of
    its PowerNorm for the band, never above it; calibration files in
    `Instrument config/aotf/`.
  - Mode = calibration: VIS (`102102 Visible.aotf`) or NIR (`102102 NIR1.aotf`),
    `calibrations = { vis, nir }`. Switch mode… swaps them in one click, with no
    interlock; nothing is sent. No wavelength limits in the config: each file's
    usable range (power bands, 1-200 MHz) is worked out from the file.
  - Control only: readings are the settings (software level), read back from
    the controller on connect; safe state = amplitude 0. Not yet run on the
    hardware.
- **General Measurement**, after Labber's Measurement Editor
  (`docs/general-measurement.md`):
  - the server's connected instruments with their Set channels (writable
    numeric definition properties, or a plain `set()`) and Read channels
    (`describe()` fields);
  - a step sequence (the first channel is the inner loop, single values are set
    once), each channel's values from step items (single, start – stop or
    center – span; points or step size; linear or log), wait after set and
    snake; log channels;
  - Check (limits and estimated time, from the panels' read times), Start
    (asks first), Pause / Resume, Stop; after the run hold or safe state;
  - data as HDF5 filed by date (`Data/YYYY/MM/Data_MMDD/<name>.h5`), with the
    measurement in the file; a live plot; measurements saved as TOML.
  - `gmcore/measurement/` (config, channels, adapters to the ordinary runner;
    no Qt). The server emits `instruments_changed` and has `shutdown_order()`,
    and won't close while a measurement runs.
- Adapters: Lake Shore 335 temperature controller and Lake Shore 625 magnet
  supply (Rampable), Giga-tronics 2420B signal generator (Movable on
  frequency or power). Read-only `configure()` checks for the Lake Shores;
  reply formats taken from the real instruments; writes not yet sent to
  hardware.
- `Instrument config/lab.toml` (was `station.lab.toml`): the instruments on
  the lab GPIB bus.
- Instrument control windows `gm gui --station ...` (`gui` extra: PySide6 +
  PySide6-QtAds + pyqtgraph). There is a station window (status, log,
  station-wide actions) and one window per instrument type; same-type
  instruments are QtAds docks in one window (tab, split, float). A device's
  `window` key in the station TOML overrides the grouping. The layout is saved
  per station. Each instrument has a live readout and trend and its essential
  controls. Safe state works per instrument, per window, or station-wide.
  Connecting is query-only
  (verified on the lab bus with writes blocked). Driver helpers it uses:
  `Keithley24xx.sync()`, `Keithley2000.sync()`, `LakeShore335.hold()` /
  `set_heater_range()`, `LakeShore625.hold()`, `Gigatronics2420B.set_freq()` /
  `set_power()` / `output_state()`, lock-in `set_sensitivity()` /
  `set_time_constant()` / `auto_phase()`.
- Cryostat panels with their own look, after the legacy LabVIEW programs:
  - Temperature (both inputs, stable lights, K/min rates, heater bar and range
    buttons, PID read only).
  - Magnet (field/current readout, limit bar with target, ramp progress and
    ETA, stable / P.S. heater / error lights, Ramp to zero).
  - Helium level (dewar gauge, in/day boil-off fit, time to the low mark,
    Sample/Hold vs Continuous, Measure now).
  - All three have charts with display ranges and CSV logging. They live in
    `gui/common/dedicated_panels/`, one module per driver, with shared widgets in
    `gui/common/widgets.py`.
- Monitor and chart windows ("New monitor…", "New chart…" in the station
  window): big numbers with optional amber/red thresholds, or a live chart of
  several instruments with a second y-axis for a second unit.
  - Any number can be open. They are saved and restored with the layout,
    fields included.
  - They are read only: values come from the panels' existing polls through a
    new `DataHub` (`gui/common/hub.py`, fed by the new `Panel.reading` signal). Extra
    windows add no driver reads.
  - `TimeChart` can add curves later and has a right-hand axis.
  - `StationWindow(reset_layout=True)` starts without the saved layout.
  - `AsyncWorker` drops work submitted after `stop()` instead of leaving an
    un-awaited coroutine.
- Driver definitions (`devices/definitions.py`, `devices/defs/*.toml`, see
  `docs/definitions.md`).
  - Each driver's properties are declared in TOML: kind, units, limits,
    access, readback, ramp (with explicit rate units), confirmations,
    interlocks, and conditions. The Python names the methods that do the
    work.
  - A setting that changes other properties' units or roles is a mode, with
    one file per mode: Keithley 24xx source function, Keithley 2000 function,
    2420B scan axis.
  - Every real driver has a definition, and its `describe()` is generated
    from it. The keys, units and labels are unchanged.
  - The Keithley 24xx, Keithley 2000, 2420B and SR830/SR850 panels are now
    one `DefinitionPanel` built from the definitions. The cryostat panels keep
    their own layouts.
  - New driver methods:
    - Keithley 24xx: `switch_mode()` (ramp to 0, output OFF, then
      `configure()` in the other function, settings checked before any I/O),
      `active_mode()`, `set_output()`, `output_on`, `programmed_level`;
    - Keithley 2000: `switch_mode()`, `set_nplc()`, `set_range()`;
    - lock-ins: `get_sensitivity()`, `get_time_constant()`;
    - LS625: `quantity`.
  - Station: `[devices.X.modes.<mode>]` on a Keithley 24xx gives the other
    function's compliance, limits and max rate, so the GUI can switch to it.
  - Every property has a `level` by how often it is used: 0 essential,
    1 expanded, 2 all. The standard panel shows level 0 and folds the others
    into nested **More** / **All** toggle lists (`gui.widgets.Fold`). The
    instrument window's **Essential / Expanded / All** buttons set every
    panel at once, and each panel's level is saved with the layout.
- Instrument Server (`python instrumentserver.py`, `gm server`,
  `gui/server/`, `docs/instrument-server.md`), the first of the two main
  GUIs (the other, General Measurement, is planned; see the project plan).
  - One row per instrument of an instrument config: address, working name,
    driver, connection light, Connect / Disconnect, Open panel.
  - Open panel picks the dedicated panel, else the standard one.
  - The File menu has Open / Save / Save as. Saving keeps the file's comments
    (`tomlkit`, added to the `gui` extra).
  - Add instrument is a placeholder.
- `Instrument config/lab.toml` and `sim.toml`: the station files with
  working names (`Keithley 2400 - 1`).
- Device names are checked: unique by construction, no `.`, `/`, `\` or
  quotes, no spaces at either end (`config.schema.check_name`).
- `config.schema.DRIVERS` / `DRIVER_DEFAULTS` and
  `config.registry.DRIVER_CLASSES`.
- `gui.registry.panel_for()` falls back to the standard panel for any driver
  with a definition.
- Window layouts are Qt Designer forms (`.ui`), loaded at runtime by
  `gui.common.uiload.load_ui()` into the window object (5 to 34 ms per window,
  mostly widget creation). See `docs/designer.md`.
  - Forms: the standard panel, the three cryostat panels, the Instrument
    Server and its panel window, the monitor/chart window, the field picker,
    and a General Measurement layout preview (Window menu of the server).
  - Our widgets are promoted widgets with Designer properties (`Led.label`,
    `TimeChart.yLabel` / `yUnit` / `curves_spec`, `SIEdit.unit`). Cryostat
    cards are QFrames with `card = true` (`widgets.CardLabels`).
  - The loader refuses object names that would replace a method.
  - `Panel.build_layout()` is replaced by `setup_ui()`, and `Panel.UI` names
    the form.
- Icons: Material Design Icons (MDI 6) through QtAwesome, in every window.
  - `gui/common/icons.py` maps roles to icons; Designer forms get theirs by
    object name when they load.
  - Toolbars keep their text beside the icons, and the panel windows get an
    instrument icon.
  - QtAwesome is added to the `gui` extra. Without it the GUI has no icons.
- Theme: a **Miscellaneous** menu with **Follow system**, **Bright** and
  **Dark** (`gui/common/theme.py`), in the Instrument Server (and the legacy
  station and bench windows); the panel windows follow it.
  - One setting for the whole application: changing it in any window changes
    them all, and it is remembered.
  - Windows use the Fusion style, because the native Windows 10 style has no
    dark colours. Icons take the text colour and are redrawn on a change.
- Panels show how long a read takes next to the Live interval (`read 85 ms`,
  the average of the last ten polls), in amber once it is 80 % of the interval
  or more. Timed around the panel's poll, so no driver changed.
- Instrument Server table: a **Safe state** button per row (greyed out while
  disconnected), and **Connect all**, **Set all safe** (shutdown order first,
  after asking) and **Disconnect all** at the right of Add instrument.
- The shortest Live interval is 0.02 s (was 0.2 s), `Panel.MIN_INTERVAL`.
- App settings file `Instrument config/misc.toml` (`gui/common/appsettings.py`,
  git-ignored): the theme, the last instrument config, whether to open it at
  start, and the Quick view's hidden readings. Readable TOML, written on each
  change.
  - The Instrument Server starts with the last config opened or saved
    (Miscellaneous → **Open the last instrument config at start**), else
    `lab.toml`.
- Instrument Server tabs: **Config** (the table and the log, as before) and
  **Quick view** (`gui/server/quick.py`).
  - The quick view stacks one block of readings per connected instrument,
    fed by the panels' polls, so it adds no instrument traffic.
  - **Settings…** gives a checkbox per reading to hide some. The hidden
    readings are kept in `Instrument config/misc.toml`.
- Keithley SMUs, **Switch mode…**: enabled whenever the instrument is
  connected and the output is OFF, whatever the level. Otherwise the driver
  refuses too.
  - It asks for the new function's compliance and limits, pre-filled from the
    station's `[devices.X.modes.<mode>]` or else from the mode file's
    `switch_defaults`. No station table is required any more.
  - Then `*RST` and configured in the new function (level 0, output off).
  - Fixed: clicking the button raised `KeyError: False`.
  - Definition keys `switch_interlock`, `switch_defaults`.
- Lock-ins: R and phase are main readings (level 0), like X and Y.
  - The reference frequency (internal reference only, harmonic × frequency ≤
    102 kHz), amplitude and phase can be set. Their bounds come from
    `srs_lockin.toml` (the driver uses the same values), and each is read back.
- Keithley SMUs: **no Python-level ramp**. `set()` writes the level in one
  step everywhere: the panel's Set, scan points, `safe_state()` (level 0, then
  output off), output Off and `switch_mode()`.
  - `max_rate` and `step_dt` are gone from the drivers, the station configs
    (`KeithleySMUCfg`, `[devices.X.modes.*]`) and the definitions. The level
    row is a plain Set, with no rate and no Stop.
  - The Lake Shore ramps, which run in the instruments, are unchanged.
- NPLC is a number box on the Keithley SMUs and the 2000, bounded by `min` /
  `max` in each model's .toml.
  - The drivers check against those same bounds (`definitions.prop_bounds()`),
    and the SMUs gain `set_nplc()`.
  - The standard panel refuses any typed value outside a property's `min` /
    `max` / `limits` before sending it.
- **Update settings** button at the top of the standard panel's Control box
  (and in the cryostat panels' control boxes): re-reads the settings from the
  instrument, queries only.
- The standard panel's charts sit in a splitter: drag between the controls and
  the charts, or between open charts, to resize. Closed charts take only their
  heading's height.
- Standard panel charts: the always-on trend is replaced by up to four folded
  charts, one per reading channel.
  - They are closed by default, and each has a channel drop-down and **Auto
    zoom** (back to the default, autoranged view). `Panel.build_charts()`,
    `TrendChart`.
  - Every channel's history (3600 polls) is kept while charts are closed;
    closed charts don't draw.
  - The history restarts after a mode switch (the units may change).
- The form loader also refuses a widget name that would replace data the code
  set before loading (it had replaced the panel's chart list).
- Rule for drivers: one instrument model, one driver module, and its own
  definition files; drivers share no code (`docs/adding-an-adapter.md`).
  - The Keithley 2400, 2401, 2410 and 2450 are now independent files
    (`keithley_<model>.py`, `defs/keithley_<model>.toml` plus `_volt`,
    `_curr`). `keithley_24xx.py`, `Keithley24xx`, `Spec24xx`, `model_class` and
    `defs/keithley_24xx*.toml` are gone. The config classes are
    `KeithleySMUCfg` / `KeithleySMUModeCfg`.
- Instrument Server: the Driver column is a two-level menu, maker then model
  (`gui.common.widgets.MenuPicker`, groups in `server.window.DRIVER_GROUPS`).
- Keithley SMUs: one driver per model, `keithley_2400`, `keithley_2401`,
  `keithley_2410`, `keithley_2450` (classes `Keithley2400` …).
  - The station's `model` key is gone (the driver name says it).
  - Each model's ranges, limits and power envelope come from Keithley's
    datasheets and manuals, and are checked when the driver is built.
  - New: `set_source_range()`, `set_sense_range()` (None = autorange), their
    getters, and `effective_compliance`. The station takes `source_range` /
    `sense_range`.
  - The GUI shows both ranges as level-1 drop-downs (model's ranges + Auto).
    The source range changes only with the output off.
  - 2401 maximum corrected to ±21 V (was ±20 V).
  - Compliance, NPLC and the power envelope are now checked.
- Every control checked against the manuals (`docs/manual-review.md`):
  - Keithley 2000: the range is a drop-down of the function's ranges; NPLC
    0.01 added.
  - SR830/SR850: the station amplitude and frequency are checked, and the time
    constant is read back.
  - LS335: output 2 in voltage mode takes heater range 0/1 only.
  - LS625: ±60.1 A and 0.0001 … 99.999 A/s are checked.
  - 2420B: 10 MHz … 20 GHz.
  - LM-510: the panel interval goes up to 99:59 h.
- `gm validate` builds every hardware driver without opening its session, so
  settings the model can't do fail without touching the bus. It prints the
  error instead of a traceback. The registry builds drivers before opening
  their sessions too.
- Definitions: an `enum` with `allow_auto` lists Auto first.
- The Python package is now `gmcore` (was `generalmeasurements`, the same
  name as the repository folder): `import gmcore`, `gmcore.load()`. The
  project name, the CLI (`gm`, `generalmeasurements`) and the HDF5 provenance
  key `generalmeasurements_version` are unchanged, so existing data files
  load as before. All Designer forms are in `gmcore/gui/forms/`; the cryostat
  panels are in `gui/common/dedicated_panels/`; the tests' configs are in
  `tests/fixtures/`.
- Reorganized for the two main GUIs:
  - `gui/` is split into `common/` (panels, widgets, worker, hub, registry,
    forms; shared), `server/` (Instrument Server), `measurement/` (General
    Measurement, preview) and `legacy/` (`gm gui` station windows, monitor and
    chart views, driver test bench). Each keeps its forms in `ui/`.
  - The core packages (`devices`, `config`, `engine`, `plans`, `sinks`) are
    unchanged.
  - `station.*.toml` moved: `Instrument config/` holds the user's configs
    (`lab`, `sim`, `example`, with working names). The tests use their own
    copies in `tests/fixtures/`, so editing or saving a config can't break them.
  - `examples/gate_sweep.py` uses the simulated config's names.
- `gmcore.units`: `fmt()` / `parse_si()` without Qt (`gui.panels`
  re-exports them).
- Adapter: Cryomagnetics LM-510 helium level meter (`lm510`, Readable).
  Unverified: the lab meter has not answered yet. The VISA transport gains
  `echo` for instruments that repeat commands on RS-232.
- `LakeShore335.read_input()`, `get_heater_range()`, `ramp_rate()`;
  `LakeShore625.target`, `switch_heater_on()` (all queries).
- Driver test bench (now `python -m gmcore.gui.legacy.bench`,
  was `guitestmain.py`). It scans GPIB with `*IDN?`,
  presets an address per driver, and opens one instrument per window.
- `config.registry.build_device()`: build one device without configuring it.
- `errors()` (drain `:SYST:ERR?`) on the Keithley adapters; `configure()`
  raises if any of its commands was rejected.
- Live hardware tests `tests/test_live_keithley.py` (`SWEEP_LIVE=1`, outputs
  off) and their transcripts in `tests/golden/`. Communication with the
  Keithley 2400, 2401 and 2000 verified on hardware.

### Fixed
- Keithley 2450 `read()` requests source and sensed values explicitly
  (`:READ? "defbuffer1", SOUR, READ`) instead of expecting `V,I`; current-source
  mode now senses voltage instead of current.
- VISA timeouts surfaced as pyvisa `VisaIOError`, which the retry policy does
  not retry, so one timeout failed the run. They are now `TimeoutError` (other
  VISA errors `DriverError`), and the transport device-clears first: on a
  Keithley 2000, the reply that arrived after a timeout was read by the next
  query (`'-KEITHLEY INSTRUMENTS...'`).
- Keithley 2000: every `:READ?` logged `-213 "Init ignored"` when the unit was
  left in continuous trigger mode; `configure()` now sends `:INIT:CONT OFF`.
- Keithley 24xx `read()` with the output off waited for a 4 s VISA timeout (the
  unit logs 803 and never replies); it now raises `DriverError` at once.
- `dry-run` treated only `sim_magnet` as Rampable.

### Tooling
- Renamed the project from `sweepkit` to GeneralMeasurements. The import
  package is `generalmeasurements` and the CLI is `gm` (or
  `generalmeasurements`). The HDF5 provenance key `sweepkit_version` is now
  `generalmeasurements_version`. The Qt binding is PySide6 (was PyQt6).
- CI (GitHub Actions): ruff, pytest and `gm validate` on Windows and
  Linux, Python 3.12 and 3.13.
- Ruff rule set pinned in `pyproject.toml` so an upgrade can't change what CI
  enforces.
- pyright runs in CI as report-only (`standard` mode). It reports 55 errors,
  almost all from untyped h5py/pyvisa returns, Optional attributes set in
  `open()`, and devices typed as `object` in plans/runner. Clear these, make
  the job blocking, then move `devices/`, `engine/`, `plans/` to strict
  (~300 more, mostly missing annotations).
- The type-check job installs the GUI extras, so the GUI code is checked
  against PySide6's types. Designer forms' widgets are declared for pyright
  in `gui/common/form_types.py`, generated from the `.ui` files
  (`python -m gmcore.gui.common.uiload`; `tests/test_ui.py` checks that it is
  up to date). `InstrumentRow` declares its table widgets. With the GUI
  packages installed, pyright went from 527 errors to 162; the rest are other
  kinds (None checks, untyped h5py/pyvisa returns, override signatures).
- Typed h5py and VISA access, down to 111 pyright errors:
  - `sinks/hdf5.py`'s `group()` / `dataset()` check what a path holds;
    `dataset.py` uses them, so a malformed file says which path is wrong.
  - The sink refuses an event before RunStart with a clear error.
  - `VisaTransport` keeps a typed `MessageBasedResource` and raises
    "not open" instead of an AttributeError on `None`.

### Known gaps
- SR850 (GPIB 8) stopped answering everything but `OUTP?` (which reads 0)
  during testing on 2026-10-16; needs a front-panel check or power cycle.
- GUI tests are skipped in CI (no Qt installed there).
- No lock-in overload detection.
- Phase 6 optical axis / array Readable and phase 8 items not started.
